#!/usr/bin/env python3
"""worker-bridge-operator: per-worker cimicode runtime pod automation.

Watches Worker CRs (agentteams.io/v1beta1) in one namespace. For every worker
whose spec.runtime is "worker-bridge" it dispatches on spec.adapterMode
(per-worker, no global stack-mode switch):

    cimicode-stateless  → zero provisioning. The bridge calls an external
                          cimicode platform directly; its binding arrives via
                          the runtime.yaml bridge section (controller
                          projection of spec.cimicodeGatewayUrl/sessionId/...).
                          This operator touches nothing.
    cimicode-pod        → own a single cimicode Deployment + Service:
                              Service     <worker>-cimicode-svc  (runtime :4096
                                                                          helper :4097)
                              Deployment  <worker>-cimicode
                              Secret      <worker>-cimicode-fs (MinIO creds,
                                          copied read-only from the bridge pod
                                          env the controller assembled)
                          (role-suffixed so pod names read at a glance: the
                          Deployment's pods are <worker>-cimicode-<rs>-<hash>)
                          and point the Worker CR spec.env at it (the bridge
                          pod picks it up via self-heal polling):
                              BRIDGE_RUNTIME_ADAPTER=cimicode-pod
                              BRIDGE_RUNTIME_BASE_URL=http://<w>-cimicode-svc.<ns>.svc:4096
                              BRIDGE_RUNTIME_HELPER_URL=http://<w>-cimicode-svc.<ns>.svc:4097

The cimicode runtime image (worker-bridge/cimicode-runtime) is a merged
single-container form: opencode serve (conversation loop, REST per the
adapter contract) + sandbox helper (AGENTS.md writes, command exec) + the
full collaboration toolchain (taskflow / agentteams-sync / mc / skills).
The operator therefore also feeds the pod its working env
(AGENTTEAMS_WORKER_NAME / FS_* / TEAM / MATRIX_USER_ID) so taskflow and mc
sync resolve team paths; credentials ride the Secret via secretKeyRef.

No sandbox pod, no hostPath: /workspace is an emptyDir (conversation state
survives container restarts; a pod recreation rebuilds sessions via the
bridge adapter's 404 self-heal — PVC is a known productionization step).

CIMICODE_IMAGE is required (no default — fail loud per pass until the env is
supplied); CIMICODE_PORT defaults to 4096 and the readiness probe to GET
/session (both pinned by the cimicode-runtime image contract). AGENTTEAMS_FS_
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
ENV_KEY_ADAPTER = "BRIDGE_RUNTIME_ADAPTER"
ENV_KEY_BASE_URL = "BRIDGE_RUNTIME_BASE_URL"
ENV_KEY_HELPER_URL = "BRIDGE_RUNTIME_HELPER_URL"

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


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


class OperatorConfig:
    def __init__(self) -> None:
        self.namespace = env("WATCH_NAMESPACE", "")
        # Required, no default: the cimicode runtime image is environment
        # specific (internal registry). Missing → per-pass error log, nothing
        # provisioned, instead of silently running a wrong/mocked image.
        self.cimicode_image = env("CIMICODE_IMAGE", "")
        # Port pair pinned by the cimicode-runtime image contract (opencode
        # serve / sandbox helper in one container).
        self.cimicode_port = int(env("CIMICODE_PORT", "4096"))
        self.cimicode_helper_port = int(env("CIMICODE_HELPER_PORT", "4097"))
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

    def cluster_dns(self, service: str) -> str:
        return f"{service}.{self.namespace}.svc.cluster.local"


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

    def worker_spec_env(self, worker_obj: dict[str, Any]) -> dict[str, str]:
        raw = worker_obj.get("spec", {}).get("env") or {}
        return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}

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
                    client.V1ServicePort(
                        name="helper",
                        port=self.cfg.cimicode_helper_port,
                        target_port=self.cfg.cimicode_helper_port,
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
    ) -> client.V1Deployment:
        name = self.cimicode_deploy_name(worker)
        # Working env for the in-pod toolchain (taskflow / mc sync). FS_* 契约
        # 同 worker-bridge/cimicode-sandbox：绝不设置 AGENTTEAMS_RUNTIME——
        # mc 同步只认 ("k8s","aliyun")，其余走 local 静态三元组模式。
        pod_env: dict[str, str] = {
            "AGENTTEAMS_FS_ROOT": "/workspace",
            "AGENTTEAMS_WORKER_NAME": worker,
            "SANDBOX_EXEC_URL": f"http://127.0.0.1:{self.cfg.cimicode_helper_port}",
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
        if with_fs_secret:
            # 凭据不经明文 env：bridge pod env 里的明文复制进 Secret，
            # 这里以 secretKeyRef 引用。
            secret_name = self.cimicode_secret_name(worker)
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
        container = client.V1Container(
            name="cimicode",
            image=self.cfg.cimicode_image,
            image_pull_policy="IfNotPresent",
            env=container_env,
            ports=[
                client.V1ContainerPort(name="runtime", container_port=self.cfg.cimicode_port),
                client.V1ContainerPort(name="helper", container_port=self.cfg.cimicode_helper_port),
            ],
            volume_mounts=[
                client.V1VolumeMount(name="workspace", mount_path="/workspace")
            ],
        )
        if self.cfg.cimicode_probe_path:
            container.readiness_probe = client.V1Probe(
                http_get=client.V1HTTPGetAction(
                    path=self.cfg.cimicode_probe_path, port=self.cfg.cimicode_port
                ),
                initial_delay_seconds=5,
                period_seconds=10,
            )
        pod_spec = client.V1PodSpec(
            containers=[container],
            # emptyDir：会话/工作区容器重启可存活；pod 重建丢失由 bridge
            # adapter 的 404 自愈 + 每 turn 重推 AGENTS.md 兜住。
            volumes=[client.V1Volume(name="workspace", empty_dir={})],
        )
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
        # `is not None` 而非 bool()：desired 侧 empty_dir 是 {}（falsy），API
        # 回读侧物化为 V1EmptyDirVolumeSource 实例（truthy）——bool() 比较会
        # 造成每 pass 幻影漂移（反复空转 replace）。
        live_volumes = [
            (v.name, v.empty_dir is not None, v.host_path is not None)
            for v in live.spec.template.spec.volumes or []
        ]
        want_volumes = [
            (v.name, v.empty_dir is not None, v.host_path is not None)
            for v in desired.spec.template.spec.volumes or []
        ]
        return (
            lc.image != dc.image
            or live_probe_path != want_probe_path
            or live_env != want_env
            or live_refs != want_refs
            or live_volumes != want_volumes
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

    def ensure_worker_env(self, worker: str, worker_obj: dict[str, Any]) -> None:
        svc_dns = self.cfg.cluster_dns(self.cimicode_svc_name(worker))
        wanted = {
            ENV_KEY_ADAPTER: ADAPTER_POD,
            ENV_KEY_BASE_URL: f"http://{svc_dns}:{self.cfg.cimicode_port}",
            ENV_KEY_HELPER_URL: f"http://{svc_dns}:{self.cfg.cimicode_helper_port}",
        }
        current = self.worker_spec_env(worker_obj)
        if all(current.get(k) == v for k, v in wanted.items()):
            return
        merged = dict(current)
        merged.update(wanted)
        body = {"spec": {"env": merged}}
        self.custom.patch_namespaced_custom_object(
            GROUP, VERSION, self.cfg.namespace, WORKERS_PLURAL, worker, body
        )
        log.info("patched Worker %s spec.env -> cimicode pod %s", worker, wanted[ENV_KEY_BASE_URL])

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
        # bridge pod env: MinIO 凭据的权威来源（controller 组装的明文 env）。
        # pod 未起（双候选都 404）→ 本轮推迟，下一轮重试——凭据到位前建
        # Secret/Deployment 只会得到一个永远连不上 FS 的 pod。
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
            self.ensure_secret(self.cimicode_secret_name(worker), worker, creds)
            with_fs_secret = True
        else:
            log.warning(
                "worker %s: AGENTTEAMS_FS_ENDPOINT not set — degraded plain-chat "
                "pod (taskflow / mc sync non-functional)",
                worker,
            )
        team = self.team_for(worker)
        matrix_user = self.matrix_user_id(worker, worker_obj)
        self.ensure_service(self.svc(worker))
        self.ensure_deployment(
            self.cimicode_deployment(
                worker, team=team, matrix_user=matrix_user, with_fs_secret=with_fs_secret
            )
        )
        self.ensure_worker_env(worker, worker_obj)
        log.info(
            "worker %s reconciled (mode=%s svc=%s team=%s fs=%s)",
            worker,
            mode,
            self.cimicode_svc_name(worker),
            team or "-",
            "secret" if with_fs_secret else "degraded",
        )

    def run(self) -> None:
        log.info(
            "worker-bridge-operator starting ns=%s cimicode=%s port=%d helper=%d interval=%ss",
            self.cfg.namespace,
            self.cfg.cimicode_image or "(CIMICODE_IMAGE unset!)",
            self.cfg.cimicode_port,
            self.cfg.cimicode_helper_port,
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
