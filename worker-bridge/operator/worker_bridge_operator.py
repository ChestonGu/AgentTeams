#!/usr/bin/env python3
"""worker-bridge-operator: per-worker runtime pod automation.

Watches Worker CRs (agentteams.io/v1beta1) in one namespace. For every worker
whose spec.runtime is "worker-bridge" it dispatches on spec.adapterMode
(per-worker, no global stack-mode switch):

    cimicode-stateless  → zero provisioning. The bridge calls an external
                          cimicode platform directly; its binding arrives via
                          the runtime.yaml bridge section (controller
                          projection of spec.runtimeParameter:
                          baseUrl/sessionId/sandboxId/templateId + any
                          extra keys, passed through to the chat body).
                          This operator touches nothing.
    cimicode-pod        → own a single runtime Deployment + Service:
                              Service     <worker>-cimicode-svc  (runtime :4096)
                              Deployment  <worker>-cimicode
                              Secret      <worker>-cimicode-fs (MinIO creds +
                                          model config, copied read-only from
                                          the bridge pod env the controller
                                          assembled)
                          (role-suffixed so pod names read at a glance: the
                          Deployment's pods are <worker>-cimicode-<rs>-<hash>)

                          The bridge derives its own wiring from the naming
                          contract (<w>-cimicode-svc:4096, gated by a GET
                          /session health probe): this operator never writes
                          the Worker CR — the pod-created-before-wiring race
                          is absorbed by the bridge's self-heal loop, not by
                          a CR patch that arrives too late anyway (the pod
                          env snapshot is immutable). Legacy BRIDGE_RUNTIME_*
                          keys left in spec.env by an older operator are
                          harmless: same values, highest priority, explicit.

The runtime image is form-agnostic: CIMICODE_IMAGE may point at either
worker-bridge/cimicode-runtime (internal coder-cimicode base image) or
worker-bridge/opencode-runtime (external opencode npm form). The two share
one outward contract — single port :4096 REST (per-turn agent.md via the
message body system field, model via OPENCODE_CONFIG_CONTENT,
OPENCODE_PERMISSION allow-all) — so this operator and the bridge treat them
identically; switching runtimes is an image tag change. The image is a
merged single-container form: <runtime> serve (conversation loop, REST per
the adapter contract) + the full collaboration toolchain (taskflow /
agentteams-sync / mc / skills). The operator therefore also feeds the pod
its working env (AGENTTEAMS_WORKER_NAME / FS_* / TEAM / MATRIX_USER_ID) so
taskflow and mc sync resolve team paths; credentials ride the Secret via
secretKeyRef.

Model injection (same source as the native worker/leader chain): Worker CR
spec.model + the bridge pod env pair AGENTTEAMS_AI_GATEWAY_URL /
AGENTTEAMS_WORKER_GATEWAY_KEY (Higress AI gateway) are rendered into the
runtime config dialect (see render_model_config) and injected as
OPENCODE_CONFIG_CONTENT via secretKeyRef — merged last by the runtime, so it
overrides every other config source. spec.model == "native-config" is a
sentinel: skip injection, image defaults apply. A missing model or missing
gateway env defers provisioning (fail loud) — never build a pod whose turns
cannot run. Config changes (model / gateway / key rotation) roll out via the
plain-text CIMICODE_MODEL_CONFIG_HASH env: Secret value changes alone do not
restart pods; the hash change drifts the pod template and ensure_deployment
replaces the spec → rolling restart.

No sandbox pod, no hostPath, no emptyDir: /workspace is the container's
writable layer. A pod recreation loses session state — covered by the bridge
adapter's 404 self-heal, the per-turn system field and taskflow's mc pull
(PVC is a known productionization step).

CIMICODE_IMAGE is required (no default — fail loud per pass until the env is
supplied); CIMICODE_PORT defaults to 4096 and the readiness probe to GET
/session (both pinned by the runtime image contract). AGENTTEAMS_FS_
ENDPOINT feeds the pod's collaboration env; when missing the operator
degrades to a plain-chat pod (warning logged — taskflow/sync non-functional).

Deleting the worker (or changing runtime away from worker-bridge /
adapterMode to cimicode-stateless) garbage-collects the stack via the
managed-by label.

Everything is level-triggered and idempotent: the loop converges rather than
tracking events, so a crashed/restarted operator self-heals on the next pass.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import sys
import time
from typing import Any

from kubernetes import client, config
from kubernetes.client.rest import ApiException

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("worker-bridge-operator")

GROUP = "agentteams.io"
VERSION = "v1beta1"
WORKERS_PLURAL = "workers"
TEAMS_PLURAL = "teams"

MANAGED_BY = "worker-bridge-operator"

ADAPTER_STATELESS = "cimicode-stateless"
ADAPTER_POD = "cimicode-pod"
RUNTIME_WORKER_BRIDGE = "worker-bridge"

# The controller-created bridge pod: agentteams-worker-<w>-bridge (newer) or
# agentteams-worker-<w> (older) — both candidates are probed for env reads.
BRIDGE_POD_PREFIX = "agentteams-worker-"
BRIDGE_POD_SUFFIX = "-bridge"

FS_ACCESS_KEY_ENV = "AGENTTEAMS_FS_ACCESS_KEY"
FS_SECRET_KEY_ENV = "AGENTTEAMS_FS_SECRET_KEY"
SECRET_KEY_ACCESS = "accessKey"
SECRET_KEY_SECRET = "secretKey"

# 模型注入（与原生 worker/leader 链路同源：controller agentconfig/generator.go
# 的 agentteams-gateway provider = Higress AI 网关 OpenAI 兼容端点）：
#   model   = Worker CR spec.model（trim "agentteams-gateway/" 前缀；
#             "native-config" 哨兵 = 不注入，镜像默认配置生效）
#   baseURL = bridge pod env AGENTTEAMS_AI_GATEWAY_URL + "/v1"
#   apiKey  = bridge pod env AGENTTEAMS_WORKER_GATEWAY_KEY（Higress consumer key）
# 渲染成 cimicode/opencode 配置方言经 OPENCODE_CONFIG_CONTENT env 注入（合并
# 优先级最高的配置源）；JSON 含 key，整体放 Secret，Deployment spec 只见
# secretKeyRef + 明文内容哈希 env——Secret 值变更不触发 pod 重启，哈希变更经
# pod template 漂移滚动生效。
AI_GATEWAY_URL_ENV = "AGENTTEAMS_AI_GATEWAY_URL"
GATEWAY_KEY_ENV = "AGENTTEAMS_WORKER_GATEWAY_KEY"
SECRET_KEY_MODEL_CONFIG = "model-config"
ENV_MODEL_CONFIG = "OPENCODE_CONFIG_CONTENT"
ENV_MODEL_CONFIG_HASH = "CIMICODE_MODEL_CONFIG_HASH"
PROVIDER_ID = "agentteams-gateway"
NATIVE_CONFIG_MODEL = "native-config"


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


class OperatorConfig:
    def __init__(self) -> None:
        self.namespace = env("WATCH_NAMESPACE", "")
        # Required, no default: the cimicode runtime image is environment
        # specific (internal registry). Missing → per-pass error log, nothing
        # provisioned, instead of silently running a wrong/mocked image.
        self.cimicode_image = env("CIMICODE_IMAGE", "")
        # Port pinned by the runtime image contract (cimicode serve / opencode
        # serve; agent.md rides the message body system field — no helper port).
        self.cimicode_port = int(env("CIMICODE_PORT", "4096"))
        # Readiness probe defaults to the contract surface (GET /session on
        # the opencode port); empty disables it.
        self.cimicode_probe_path = env("CIMICODE_PROBE_PATH", "/session")
        # Collaboration storage for the pod's taskflow / mc sync env. Empty
        # endpoint → degraded plain-chat pod (warning per pass).
        self.fs_endpoint = env("AGENTTEAMS_FS_ENDPOINT", "")
        self.fs_bucket = env("AGENTTEAMS_FS_BUCKET", "agentteams-storage")
        # Fallback only for AGENTTEAMS_MATRIX_USER_ID when the Worker CR
        # status has not recorded it yet: @<worker>:<MATRIX_SERVER_NAME>.
        self.matrix_server_name = env("MATRIX_SERVER_NAME", "")
        self.interval = int(env("RECONCILE_INTERVAL", "10"))
        # Effective runtime for workers whose spec.runtime is empty. Default
        # empty: this operator only acts on explicitly worker-bridge CRs.
        self.default_runtime = env("DEFAULT_RUNTIME", "")
        # Node hostname to pin provisioned cimicode Deployments to (single-node
        # or homogeneous clusters). Empty → leave scheduling to the scheduler.
        self.provision_node_selector = env("PROVISION_NODE_SELECTOR", "")


def labels_for(worker: str) -> dict[str, str]:
    return {
        "app.kubernetes.io/managed-by": MANAGED_BY,
        "app.kubernetes.io/owner": worker,
    }


def env_list(envs: dict[str, str]) -> list[client.V1EnvVar]:
    # V1EnvVar (not plain dicts) so drift comparison sees the same types as
    # objects read back from the API.
    return [client.V1EnvVar(name=k, value=v) for k, v in envs.items()]


class StackOperator:
    def __init__(self, cfg: OperatorConfig) -> None:
        self.cfg = cfg
        self.custom = client.CustomObjectsApi()
        self.core = client.CoreV1Api()
        self.apps = client.AppsV1Api()

    # ------------------------------------------------------------------
    # inventory
    # ------------------------------------------------------------------

    def bridge_workers(self) -> dict[str, dict[str, Any]]:
        items = self.custom.list_namespaced_custom_object(
            GROUP, VERSION, self.cfg.namespace, WORKERS_PLURAL
        ).get("items", [])
        return {
            w["metadata"]["name"]: w
            for w in items
            if (w.get("spec", {}).get("runtime") or self.cfg.default_runtime)
            == RUNTIME_WORKER_BRIDGE
        }

    def adapter_mode(self, worker_obj: dict[str, Any]) -> str:
        mode = str(worker_obj.get("spec", {}).get("adapterMode") or "").strip()
        # Empty → cimicode-pod, mirroring the controller's projection
        # normalization (empty adapterMode with a binding → cimicode-pod).
        return mode or ADAPTER_POD

    def team_for(self, worker: str) -> str:
        """Team CR 名反查（workerMembers 按名字匹配；无 team 返回空）。

        存储侧的 team 名 = Team CR 名去掉 bucket 前缀（mc_sync.py 语义）；
        pod 内无 agt CLI，AGENTTEAMS_TEAM 是 team 路径解析的必需 env。
        """
        teams = self.custom.list_namespaced_custom_object(
            GROUP, VERSION, self.cfg.namespace, TEAMS_PLURAL
        ).get("items", [])
        for team in teams:
            for member in team.get("spec", {}).get("workerMembers", []) or []:
                if member.get("name") == worker:
                    return team["metadata"]["name"]
        return ""

    def matrix_user_id(self, worker: str, worker_obj: dict[str, Any]) -> str:
        """权威来源 = Worker CR status.matrixUserID（controller 写入）；
        空时用 @<worker>:<MATRIX_SERVER_NAME> 兜底（仅显示/所有权默认）。"""
        recorded = str((worker_obj.get("status") or {}).get("matrixUserID") or "")
        if recorded:
            return recorded
        if self.cfg.matrix_server_name:
            return f"@{worker}:{self.cfg.matrix_server_name}"
        return ""

    def bridge_pod_env(self, worker: str) -> dict[str, str] | None:
        """读 controller 创建的 bridge pod env（明文值）。

        pod 名双候选（controller 命名演进）：agentteams-worker-<w>-bridge /
        agentteams-worker-<w>。都不存在时返回 None——bridge 还没起（worker
        仍在供给中），依赖凭据的工作推迟到下一轮 reconcile。
        """
        for name in (
            f"{BRIDGE_POD_PREFIX}{worker}{BRIDGE_POD_SUFFIX}",
            f"{BRIDGE_POD_PREFIX}{worker}",
        ):
            try:
                pod = self.core.read_namespaced_pod(name, self.cfg.namespace)
            except ApiException as exc:
                if exc.status != 404:
                    raise
                continue
            envs: dict[str, str] = {}
            for container in pod.spec.containers:
                for e in container.env or []:
                    if e.value is not None:
                        envs[e.name] = e.value
            return envs
        return None

    def fs_credentials(self, bridge_env: dict[str, str]) -> dict[str, str]:
        creds = {
            SECRET_KEY_ACCESS: bridge_env.get(FS_ACCESS_KEY_ENV, ""),
            SECRET_KEY_SECRET: bridge_env.get(FS_SECRET_KEY_ENV, ""),
        }
        return creds if all(creds.values()) else {}

    # ------------------------------------------------------------------
    # model config（cimicode/opencode 配置方言，OPENCODE_CONFIG_CONTENT 消费）
    # ------------------------------------------------------------------

    def model_config_content(
        self, worker_obj: dict[str, Any], bridge_env: dict[str, str]
    ) -> tuple[str | None, str]:
        """渲染模型/provider 配置 JSON。

        返回 (content, problem)：
          content 非 None         → 注入（problem 恒空）
          content None, problem   → 推迟供给（fail-loud：模型缺失或网关要素
                                    不在 bridge pod env——先别建一个跑不起来
                                    turn 的 pod）
          content None, problem空 → 合法跳过（native-config 哨兵：镜像默认
                                    配置生效，与 controller isNativeConfigModel
                                    语义一致——EqualFold + TrimSpace）
        """
        spec_model = str(worker_obj.get("spec", {}).get("model") or "").strip()
        if not spec_model:
            return None, "spec.model is empty"
        if spec_model.lower() == NATIVE_CONFIG_MODEL:
            return None, ""
        gateway_url = (bridge_env.get(AI_GATEWAY_URL_ENV) or "").strip()
        gateway_key = (bridge_env.get(GATEWAY_KEY_ENV) or "").strip()
        missing = [
            name
            for name, value in (
                (AI_GATEWAY_URL_ENV, gateway_url),
                (GATEWAY_KEY_ENV, gateway_key),
            )
            if not value
        ]
        if missing:
            return None, "bridge pod env carries no " + "/".join(missing)
        return self.render_model_config(spec_model, gateway_url, gateway_key), ""

    @staticmethod
    def render_model_config(model: str, gateway_url: str, gateway_key: str) -> str:
        """配置方言：provider agentteams-gateway（openai-compatible）指向
        Higress AI 网关 /v1，model 主键 agentteams-gateway/<model>——与
        openclaw 配置/runtime.yaml desired.model 的原生链路同型。
        URL 归一化（rstrip 尾斜杠）只在此处做，调用侧只 strip 空白。"""
        model = model.removeprefix(f"{PROVIDER_ID}/")
        base_url = gateway_url.rstrip("/")
        config = {
            "model": f"{PROVIDER_ID}/{model}",
            "provider": {
                PROVIDER_ID: {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "AgentTeams AI Gateway",
                    "options": {"baseURL": f"{base_url}/v1", "apiKey": gateway_key},
                    "models": {model: {"name": model}},
                }
            },
        }
        return json.dumps(config, ensure_ascii=False)

    # ------------------------------------------------------------------
    # desired objects
    # ------------------------------------------------------------------

    def svc(self, worker: str) -> client.V1Service:
        # selector targets the DEPLOYMENT's pod label (app=<deployment name>),
        # which differs from the service's own name (…-svc suffix).
        name = self.cimicode_svc_name(worker)
        return client.V1Service(
            metadata=client.V1ObjectMeta(
                name=name,
                namespace=self.cfg.namespace,
                labels=labels_for(worker),
            ),
            spec=client.V1ServiceSpec(
                selector={"app": self.cimicode_deploy_name(worker)},
                ports=[
                    client.V1ServicePort(
                        name="runtime",
                        port=self.cfg.cimicode_port,
                        target_port=self.cfg.cimicode_port,
                    ),
                ],
            ),
        )

    def cimicode_svc_name(self, worker: str) -> str:
        return f"{worker}-cimicode-svc"

    def cimicode_deploy_name(self, worker: str) -> str:
        return f"{worker}-cimicode"

    def cimicode_secret_name(self, worker: str) -> str:
        return f"{worker}-cimicode-fs"

    def cimicode_deployment(
        self,
        worker: str,
        *,
        team: str = "",
        matrix_user: str = "",
        with_fs_secret: bool = False,
        model_config: str | None = None,
    ) -> client.V1Deployment:
        name = self.cimicode_deploy_name(worker)
        # Working env for the in-pod toolchain (taskflow / mc sync). FS_* 契约
        # 同 worker-bridge/cimicode-sandbox：绝不设置 AGENTTEAMS_RUNTIME——
        # mc 同步只认 ("k8s","aliyun")，其余走 local 静态三元组模式。
        pod_env: dict[str, str] = {
            "AGENTTEAMS_FS_ROOT": "/workspace",
            "AGENTTEAMS_WORKER_NAME": worker,
            "OPENCODE_PORT": str(self.cfg.cimicode_port),
        }
        if self.cfg.fs_endpoint:
            pod_env["AGENTTEAMS_FS_ENDPOINT"] = self.cfg.fs_endpoint
            pod_env["AGENTTEAMS_FS_BUCKET"] = self.cfg.fs_bucket
        if team:
            pod_env["AGENTTEAMS_TEAM"] = team
        if matrix_user:
            pod_env["AGENTTEAMS_MATRIX_USER_ID"] = matrix_user
        container_env = env_list(pod_env)
        secret_name = self.cimicode_secret_name(worker)
        if with_fs_secret:
            # 凭据不经明文 env：bridge pod env 里的明文复制进 Secret，
            # 这里以 secretKeyRef 引用。
            container_env += [
                client.V1EnvVar(
                    name=FS_ACCESS_KEY_ENV,
                    value_from=client.V1EnvVarSource(
                        secret_key_ref=client.V1SecretKeySelector(
                            name=secret_name, key=SECRET_KEY_ACCESS
                        )
                    ),
                ),
                client.V1EnvVar(
                    name=FS_SECRET_KEY_ENV,
                    value_from=client.V1EnvVarSource(
                        secret_key_ref=client.V1SecretKeySelector(
                            name=secret_name, key=SECRET_KEY_SECRET
                        )
                    ),
                ),
            ]
        if model_config is not None:
            # 模型配置整段（含网关 key）走 Secret + secretKeyRef；明文哈希 env
            # 让内容变更（模型/网关/key 轮转）体现为 pod template 漂移 → 滚动
            # 重启生效（Secret 值变更本身不触发 pod 重启）。
            container_env += [
                client.V1EnvVar(
                    name=ENV_MODEL_CONFIG_HASH,
                    value=hashlib.sha256(model_config.encode("utf-8")).hexdigest()[:16],
                ),
                client.V1EnvVar(
                    name=ENV_MODEL_CONFIG,
                    value_from=client.V1EnvVarSource(
                        secret_key_ref=client.V1SecretKeySelector(
                            name=secret_name, key=SECRET_KEY_MODEL_CONFIG
                        )
                    ),
                ),
            ]
        container = client.V1Container(
            name="cimicode",
            image=self.cfg.cimicode_image,
            image_pull_policy="IfNotPresent",
            env=container_env,
            ports=[
                client.V1ContainerPort(name="runtime", container_port=self.cfg.cimicode_port),
            ],
            # 无 emptyDir/hostPath：/workspace = 容器可写层。pod 重建丢会话由
            # bridge adapter 的 404 自愈 + 每 turn 重发 system 兜住（容器重启
            # 也丢——按部署决策接受，PVC 化仍是生产化步骤）。
        )
        if self.cfg.cimicode_probe_path:
            container.readiness_probe = client.V1Probe(
                http_get=client.V1HTTPGetAction(
                    path=self.cfg.cimicode_probe_path, port=self.cfg.cimicode_port
                ),
                initial_delay_seconds=5,
                period_seconds=10,
            )
        pod_spec = client.V1PodSpec(containers=[container])
        if self.cfg.provision_node_selector:
            pod_spec.node_selector = {
                "kubernetes.io/hostname": self.cfg.provision_node_selector
            }
        return client.V1Deployment(
            metadata=client.V1ObjectMeta(
                name=name,
                namespace=self.cfg.namespace,
                labels=labels_for(worker),
            ),
            spec=client.V1DeploymentSpec(
                replicas=1,
                selector=client.V1LabelSelector(match_labels={"app": name}),
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(labels={"app": name}),
                    spec=pod_spec,
                ),
            ),
        )

    # ------------------------------------------------------------------
    # idempotent apply helpers (compare-then-write; never churn on equality)
    # ------------------------------------------------------------------

    def ensure_service(self, desired: client.V1Service) -> None:
        try:
            live = self.core.read_namespaced_service(desired.metadata.name, self.cfg.namespace)
        except ApiException as exc:
            if exc.status != 404:
                raise
            self.core.create_namespaced_service(self.cfg.namespace, desired)
            log.info("created Service %s", desired.metadata.name)
            return
        live_sel = live.spec.selector or {}
        want_sel = desired.spec.selector or {}
        live_ports = [(p.name, p.port, p.target_port) for p in live.spec.ports or []]
        want_ports = [(p.name, p.port, p.target_port) for p in desired.spec.ports or []]
        if live_sel != want_sel or live_ports != want_ports:
            live.spec.selector = want_sel
            live.spec.ports = desired.spec.ports
            self.core.patch_namespaced_service(
                desired.metadata.name, self.cfg.namespace, live
            )
            log.info("updated Service %s (selector/ports drift)", desired.metadata.name)

    @staticmethod
    def _env_fingerprint(container: client.V1Container) -> tuple[dict[str, str], frozenset[str]]:
        """明文 env map + secretKeyRef 引用集合（drift 比较用）。"""
        plain: dict[str, str] = {}
        secret_refs: set[str] = set()
        for e in container.env or []:
            if e.value is not None:
                plain[e.name] = e.value
            elif e.value_from and e.value_from.secret_key_ref:
                secret_refs.add(f"{e.name}->{e.value_from.secret_key_ref.name}/{e.value_from.secret_key_ref.key}")
        return plain, frozenset(secret_refs)

    def deployment_drift(self, live: client.V1Deployment, desired: client.V1Deployment) -> bool:
        lc = live.spec.template.spec.containers[0]
        dc = desired.spec.template.spec.containers[0]
        live_probe_path = (
            lc.readiness_probe.http_get.path if lc.readiness_probe and lc.readiness_probe.http_get else ""
        )
        want_probe_path = (
            dc.readiness_probe.http_get.path if dc.readiness_probe and dc.readiness_probe.http_get else ""
        )
        live_env, live_refs = self._env_fingerprint(lc)
        want_env, want_refs = self._env_fingerprint(dc)
        # 无卷可比（/workspace = 容器可写层）：漂移面 = image + probe + env +
        # secretKeyRef 引用 + nodeSelector。Secret 值变更不重启 pod——模型/
        # 网关/key 轮转靠 CIMICODE_MODEL_CONFIG_HASH 明文 env 变更入 env 指纹。
        return (
            lc.image != dc.image
            or live_probe_path != want_probe_path
            or live_env != want_env
            or live_refs != want_refs
            or live.spec.template.spec.node_selector != desired.spec.template.spec.node_selector
        )

    def ensure_deployment(self, desired: client.V1Deployment) -> None:
        name = desired.metadata.name
        try:
            live = self.apps.read_namespaced_deployment(name, self.cfg.namespace)
        except ApiException as exc:
            if exc.status != 404:
                raise
            self.apps.create_namespaced_deployment(self.cfg.namespace, desired)
            log.info("created Deployment %s", name)
            return
        if self.deployment_drift(live, desired):
            live.spec = desired.spec
            self.apps.replace_namespaced_deployment(name, self.cfg.namespace, live)
            log.info("updated Deployment %s (drift)", name)

    def ensure_secret(self, name: str, worker: str, data: dict[str, str]) -> None:
        secret = client.V1Secret(
            metadata=client.V1ObjectMeta(
                name=name, namespace=self.cfg.namespace, labels=labels_for(worker)
            ),
            string_data=data,
        )
        try:
            live = self.core.read_namespaced_secret(name, self.cfg.namespace)
        except ApiException as exc:
            if exc.status != 404:
                raise
            self.core.create_namespaced_secret(self.cfg.namespace, secret)
            log.info("created Secret %s", name)
            return
        live_data = {k: base64.b64decode(v).decode() for k, v in (live.data or {}).items()}
        if live_data != data:
            self.core.replace_namespaced_secret(name, self.cfg.namespace, secret)
            log.info("updated Secret %s (credentials rotated)", name)

    # ------------------------------------------------------------------
    # garbage collection
    # ------------------------------------------------------------------

    def garbage_collect(self, live_workers: dict[str, dict[str, Any]]) -> None:
        selector = f"app.kubernetes.io/managed-by={MANAGED_BY}"

        def orphaned(obj: Any) -> bool:
            owner = obj.metadata.labels.get("app.kubernetes.io/owner", "")
            if not owner:
                return False
            if owner not in live_workers:
                return True  # worker deleted, or runtime switched away
            # Worker alive but switched to cimicode-stateless: this operator
            # owns nothing for it anymore.
            return self.adapter_mode(live_workers[owner]) == ADAPTER_STATELESS

        for deploy in self.apps.list_namespaced_deployment(
            self.cfg.namespace, label_selector=selector
        ).items:
            if orphaned(deploy):
                self.apps.delete_namespaced_deployment(deploy.metadata.name, self.cfg.namespace)
                log.info("gc Deployment %s", deploy.metadata.name)
        for svc in self.core.list_namespaced_service(
            self.cfg.namespace, label_selector=selector
        ).items:
            if orphaned(svc):
                self.core.delete_namespaced_service(svc.metadata.name, self.cfg.namespace)
                log.info("gc Service %s", svc.metadata.name)
        for secret in self.core.list_namespaced_secret(
            self.cfg.namespace, label_selector=selector
        ).items:
            if orphaned(secret):
                self.core.delete_namespaced_secret(secret.metadata.name, self.cfg.namespace)
                log.info("gc Secret %s", secret.metadata.name)

    # ------------------------------------------------------------------
    # reconcile
    # ------------------------------------------------------------------

    def reconcile(self) -> None:
        workers = self.bridge_workers()
        self.garbage_collect(workers)
        for worker, worker_obj in sorted(workers.items()):
            try:
                self.reconcile_worker(worker, worker_obj)
            except Exception:
                log.exception("reconcile failed for worker %s", worker)

    def reconcile_worker(self, worker: str, worker_obj: dict[str, Any]) -> None:
        mode = self.adapter_mode(worker_obj)
        if mode == ADAPTER_STATELESS:
            # Zero provisioning: binding flows through the runtime.yaml
            # bridge section (controller projection), not through us.
            log.debug("worker %s: cimicode-stateless — nothing to provision", worker)
            return
        if mode != ADAPTER_POD:
            log.warning("worker %s: unknown adapterMode %r — skipping", worker, mode)
            return
        if not self.cfg.cimicode_image:
            log.error(
                "worker %s: CIMICODE_IMAGE is not set — provisioning deferred "
                "(set it in the operator deployment env)",
                worker,
            )
            return
        # bridge pod env: MinIO 凭据与模型网关要素的权威来源（controller 组装
        # 的明文 env）。pod 未起（双候选都 404）→ 本轮推迟，下一轮重试——凭据
        # 到位前建 Secret/Deployment 只会得到一个永远连不上 FS 的 pod。
        bridge_env = self.bridge_pod_env(worker)
        if bridge_env is None:
            log.info(
                "worker %s: bridge pod not found yet — provisioning deferred to next pass",
                worker,
            )
            return
        creds = self.fs_credentials(bridge_env)
        with_fs_secret = False
        if self.cfg.fs_endpoint:
            if not creds:
                log.warning(
                    "worker %s: bridge pod env carries no %s/%s — provisioning "
                    "deferred (taskflow/mc sync needs them)",
                    worker,
                    FS_ACCESS_KEY_ENV,
                    FS_SECRET_KEY_ENV,
                )
                return
            with_fs_secret = True
        else:
            log.warning(
                "worker %s: AGENTTEAMS_FS_ENDPOINT not set — degraded plain-chat "
                "pod (taskflow / mc sync non-functional)",
                worker,
            )
        # 模型注入（与原生 worker/leader 链路同源，见 model_config_content）：
        # 缺模型/缺网关要素 → fail-loud 推迟，不建跑不起来 turn 的 pod。
        model_config, problem = self.model_config_content(worker_obj, bridge_env)
        if problem:
            log.error(
                "worker %s: model config unavailable — provisioning deferred (%s)",
                worker,
                problem,
            )
            return
        # Secret：FS 凭据 + 模型配置（均"明文不进 Deployment spec"）。
        secret_data: dict[str, str] = dict(creds) if with_fs_secret else {}
        if model_config is not None:
            secret_data[SECRET_KEY_MODEL_CONFIG] = model_config
        if secret_data:
            self.ensure_secret(self.cimicode_secret_name(worker), worker, secret_data)
        team = self.team_for(worker)
        matrix_user = self.matrix_user_id(worker, worker_obj)
        self.ensure_service(self.svc(worker))
        self.ensure_deployment(
            self.cimicode_deployment(
                worker,
                team=team,
                matrix_user=matrix_user,
                with_fs_secret=with_fs_secret,
                model_config=model_config,
            )
        )
        # 不 patch Worker CR：bridge 从 svc 命名契约自行推导接线（见模块
        # docstring）——CR env 回写追不上 pod 创建竞态，且 pod env 快照不可变。
        log.info(
            "worker %s reconciled (mode=%s svc=%s team=%s fs=%s model=%s)",
            worker,
            mode,
            self.cimicode_svc_name(worker),
            team or "-",
            "secret" if with_fs_secret else "degraded",
            "injected" if model_config is not None else "native-config",
        )

    def run(self) -> None:
        log.info(
            "worker-bridge-operator starting ns=%s cimicode=%s port=%d interval=%ss",
            self.cfg.namespace,
            self.cfg.cimicode_image or "(CIMICODE_IMAGE unset!)",
            self.cfg.cimicode_port,
            self.cfg.interval,
        )
        while True:
            try:
                self.reconcile()
            except Exception:
                log.exception("reconcile pass failed")
            time.sleep(self.cfg.interval)


def main() -> int:
    cfg = OperatorConfig()
    if not cfg.namespace:
        log.error("WATCH_NAMESPACE is required")
        return 1
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    StackOperator(cfg).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
