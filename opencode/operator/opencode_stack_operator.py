#!/usr/bin/env python3
"""opencode-stack-operator: per-worker opencode + sandbox stack automation.

Watches Worker CRs (agentteams.io/v1beta1) in one namespace. For every worker
whose spec.runtime is "opencode" it owns a dedicated stack:

    Service       opencode-<worker>-svc          (4096, opencode server)
    Service       opencode-<worker>-sandbox-svc  (4097, helper HTTP /exec)
    Deployment    opencode-<worker>              (conversation loop server)
    Deployment    opencode-<worker>-sandbox      (tool chain + skills)
    Secret        opencode-sandbox-fs-<worker>   (this worker's MinIO user)

and points the Worker CR spec.env at it (the controller rolls the bridge pod):

    BRIDGE_RUNTIME_ADAPTER=opencode
    BRIDGE_RUNTIME_BASE_URL=http://opencode-<worker>-svc.<ns>.svc:4096
    BRIDGE_RUNTIME_HELPER_URL=http://opencode-<worker>-sandbox-svc.<ns>:4097

The bridge pod itself stays controller-managed — this operator only adds the
standalone runtime/sandbox pair the bridge calls (contract §0: the sandbox is
a self-built service called BY opencode). MinIO credentials are read from the
controller-created bridge Pod env (plain values) and copied into the sandbox
Secret: the sandbox is NOT controller-managed and receives nothing
automatically (contract §4.1 filesync env contract).

AGENTTEAMS_TEAM on the sandbox follows Team membership — resolved from Team CR
workerMembers on every reconcile, so a worker added to its first team after
creation picks up the right shared root without manual edits.

Deleting the worker (or changing runtime away from opencode) garbage-collects
the whole stack via the managed-by label.

Everything is level-triggered and idempotent: the loop converges rather than
tracking events, so a crashed/restarted operator self-heals on the next pass.
"""

from __future__ import annotations

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
log = logging.getLogger("opencode-stack-operator")

GROUP = "agentteams.io"
VERSION = "v1beta1"
WORKERS_PLURAL = "workers"
TEAMS_PLURAL = "teams"

MANAGED_BY = "opencode-stack-operator"
BRIDGE_POD_PREFIX = "agentteams-worker-"
ENV_KEY_ADAPTER = "BRIDGE_RUNTIME_ADAPTER"
ENV_KEY_BASE_URL = "BRIDGE_RUNTIME_BASE_URL"
ENV_KEY_HELPER_URL = "BRIDGE_RUNTIME_HELPER_URL"


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


class OperatorConfig:
    def __init__(self) -> None:
        self.namespace = env("WATCH_NAMESPACE", "opencode-team-test")
        self.opencode_image = env("OPENCODE_IMAGE", "agentteams/opencode-runtime:v0.1.1-oct")
        self.sandbox_image = env("SANDBOX_IMAGE", "agentteams/opencode-sandbox:v0.1.0-oct")
        self.fs_endpoint = env(
            "AGENTTEAMS_FS_ENDPOINT",
            "http://agentteams-oct-minio.opencode-team-test.svc.cluster.local:9000",
        )
        self.fs_bucket = env("AGENTTEAMS_FS_BUCKET", "agentteams-storage")
        self.matrix_server = env(
            "MATRIX_SERVER_NAME",
            "agentteams-oct-synapse.opencode-team-test.svc.cluster.local",
        )
        self.node_hostname = env("NODE_SELECTOR_HOSTNAME")  # empty = no selector
        self.hostpath_root = env("HOSTPATH_ROOT", "/data/oct-workers")
        self.interval = int(env("RECONCILE_INTERVAL", "10"))
        # Effective runtime for workers whose spec.runtime is empty: the
        # controller resolves empties via AGENTTEAMS_DEFAULT_WORKER_RUNTIME
        # (opencode in this namespace), but that defaulting only happens in
        # its REST create path — CRs created by other means keep the field
        # empty, so mirror the controller's default here.
        self.default_runtime = env("DEFAULT_RUNTIME", "opencode")

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

    def opencode_workers(self) -> dict[str, dict[str, Any]]:
        items = self.custom.list_namespaced_custom_object(
            GROUP, VERSION, self.cfg.namespace, WORKERS_PLURAL
        ).get("items", [])
        return {
            w["metadata"]["name"]: w
            for w in items
            if (w.get("spec", {}).get("runtime") or self.cfg.default_runtime) == "opencode"
        }

    def team_for(self, worker: str) -> str:
        teams = self.custom.list_namespaced_custom_object(
            GROUP, VERSION, self.cfg.namespace, TEAMS_PLURAL
        ).get("items", [])
        for team in teams:
            for member in team.get("spec", {}).get("workerMembers", []) or []:
                if member.get("name") == worker:
                    return team["metadata"]["name"]
        return ""

    def bridge_pod_env(self, worker: str) -> dict[str, str] | None:
        """Read the controller-created bridge Pod env (plain values).

        Returns None while the pod does not exist yet (e.g. worker still
        provisioning or controller mid-rollout) — creds-dependent work is
        deferred to the next reconcile pass.
        """
        name = BRIDGE_POD_PREFIX + worker
        try:
            pod = self.core.read_namespaced_pod(name, self.cfg.namespace)
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise
        envs: dict[str, str] = {}
        for container in pod.spec.containers:
            for e in container.env or []:
                if e.value is not None:
                    envs[e.name] = e.value
        return envs

    def worker_spec_env(self, worker_obj: dict[str, Any]) -> dict[str, str]:
        raw = worker_obj.get("spec", {}).get("env") or {}
        return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}

    # ------------------------------------------------------------------
    # desired objects
    # ------------------------------------------------------------------

    def svc(self, worker: str, name: str, app: str, port: int) -> client.V1Service:
        # selector targets the DEPLOYMENT's pod label (app=<deployment name>),
        # which differs from the service's own name (…-svc suffix).
        return client.V1Service(
            metadata=client.V1ObjectMeta(
                name=name,
                namespace=self.cfg.namespace,
                labels=labels_for(worker),
            ),
            spec=client.V1ServiceSpec(
                selector={"app": app},
                ports=[client.V1ServicePort(name="http", port=port, target_port=port)],
            ),
        )

    def runtime_deployment(self, worker: str) -> client.V1Deployment:
        name = f"opencode-{worker}"
        node_selector = (
            {"kubernetes.io/hostname": self.cfg.node_hostname}
            if self.cfg.node_hostname
            else None
        )
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
                    spec=client.V1PodSpec(
                        node_selector=node_selector,
                        containers=[
                            client.V1Container(
                                name="opencode",
                                image=self.cfg.opencode_image,
                                image_pull_policy="IfNotPresent",
                                env=env_list(
                                    {
                                        "OPENCODE_PORT": "4096",
                                        "AGENTTEAMS_FS_ROOT": "/workspace",
                                        "SANDBOX_EXEC_URL": f"http://{self.cfg.cluster_dns(f'opencode-{worker}-sandbox-svc')}:4097",
                                    }
                                ),
                                ports=[client.V1ContainerPort(name="http", container_port=4096)],
                                volume_mounts=[client.V1VolumeMount(name="workspace", mount_path="/workspace")],
                                readiness_probe=client.V1Probe(
                                    http_get=client.V1HTTPGetAction(path="/session", port=4096),
                                    initial_delay_seconds=5,
                                    period_seconds=10,
                                ),
                            )
                        ],
                        volumes=[
                            client.V1Volume(
                                name="workspace",
                                host_path=client.V1HostPathVolumeSource(
                                    path=f"{self.cfg.hostpath_root}/{worker}/workspace",
                                    type="DirectoryOrCreate",
                                ),
                            )
                        ],
                    ),
                ),
            ),
        )

    def sandbox_deployment(self, worker: str, team: str, matrix_user: str) -> client.V1Deployment:
        name = f"opencode-{worker}-sandbox"
        node_selector = (
            {"kubernetes.io/hostname": self.cfg.node_hostname}
            if self.cfg.node_hostname
            else None
        )
        sandbox_env: dict[str, str] = {
            "AGENTTEAMS_FS_ROOT": "/workspace",
            "AGENTTEAMS_WORKER_NAME": worker,
            "AGENTTEAMS_FS_ENDPOINT": self.cfg.fs_endpoint,
            "AGENTTEAMS_FS_BUCKET": self.cfg.fs_bucket,
        }
        if team:
            # storage team name = Team CR name with the bucket prefix already
            # stripped (mc_sync.py semantics); without agt CLI in the sandbox
            # this env is REQUIRED or team path resolution throws.
            sandbox_env["AGENTTEAMS_TEAM"] = team
        if matrix_user:
            # taskflow --actor / ownership guard default
            sandbox_env["AGENTTEAMS_MATRIX_USER_ID"] = matrix_user
        secret_name = f"opencode-sandbox-fs-{worker}"
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
                    spec=client.V1PodSpec(
                        node_selector=node_selector,
                        containers=[
                            client.V1Container(
                                name="sandbox",
                                image=self.cfg.sandbox_image,
                                image_pull_policy="IfNotPresent",
                                env=env_list(sandbox_env)
                                + [
                                    client.V1EnvVar(
                                        name="AGENTTEAMS_FS_ACCESS_KEY",
                                        value_from=client.V1EnvVarSource(
                                            secret_key_ref=client.V1SecretKeySelector(
                                                name=secret_name, key="accessKey"
                                            )
                                        ),
                                    ),
                                    client.V1EnvVar(
                                        name="AGENTTEAMS_FS_SECRET_KEY",
                                        value_from=client.V1EnvVarSource(
                                            secret_key_ref=client.V1SecretKeySelector(
                                                name=secret_name, key="secretKey"
                                            )
                                        ),
                                    ),
                                ],
                                ports=[client.V1ContainerPort(name="helper", container_port=4097)],
                                volume_mounts=[client.V1VolumeMount(name="workspace", mount_path="/workspace")],
                                readiness_probe=client.V1Probe(
                                    http_get=client.V1HTTPGetAction(path="/healthz", port=4097),
                                    initial_delay_seconds=3,
                                    period_seconds=10,
                                ),
                            )
                        ],
                        volumes=[
                            client.V1Volume(
                                name="workspace",
                                host_path=client.V1HostPathVolumeSource(
                                    path=f"{self.cfg.hostpath_root}/{worker}/workspace",
                                    type="DirectoryOrCreate",
                                ),
                            )
                        ],
                    ),
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
        live_ports = [(p.port, p.target_port) for p in live.spec.ports or []]
        want_ports = [(p.port, p.target_port) for p in desired.spec.ports or []]
        if live_sel != want_sel or live_ports != want_ports:
            live.spec.selector = want_sel
            live.spec.ports = desired.spec.ports
            self.core.patch_namespaced_service(
                desired.metadata.name, self.cfg.namespace, live
            )
            log.info("updated Service %s (selector/ports drift)", desired.metadata.name)

    def deployment_drift(self, live: client.V1Deployment, desired: client.V1Deployment) -> bool:
        lc = live.spec.template.spec.containers[0]
        dc = desired.spec.template.spec.containers[0]
        live_env = {e.name: e.value for e in lc.env or [] if e.value is not None}
        want_env = {e.name: e.value for e in dc.env or [] if e.value is not None}
        live_vp = live.spec.template.spec.volumes[0].host_path.path if live.spec.template.spec.volumes else ""
        want_vp = desired.spec.template.spec.volumes[0].host_path.path if desired.spec.template.spec.volumes else ""
        return (
            lc.image != dc.image
            or live_env != want_env
            or live_vp != want_vp
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
        import base64

        live_data = {k: base64.b64decode(v).decode() for k, v in (live.data or {}).items()}
        if live_data != data:
            self.core.replace_namespaced_secret(name, self.cfg.namespace, secret)
            log.info("updated Secret %s (credentials rotated)", name)

    def ensure_worker_env(self, worker: str, worker_obj: dict[str, Any]) -> None:
        base_svc = f"opencode-{worker}-svc"
        sandbox_svc = f"opencode-{worker}-sandbox-svc"
        wanted = {
            ENV_KEY_ADAPTER: "opencode",
            ENV_KEY_BASE_URL: f"http://{self.cfg.cluster_dns(base_svc)}:4096",
            ENV_KEY_HELPER_URL: f"http://{self.cfg.cluster_dns(sandbox_svc)}:4097",
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
        log.info("patched Worker %s spec.env -> opencode stack", worker)

    # ------------------------------------------------------------------
    # garbage collection
    # ------------------------------------------------------------------

    def garbage_collect(self, live_workers: dict[str, dict[str, Any]]) -> None:
        selector = f"app.kubernetes.io/managed-by={MANAGED_BY}"
        for deploy in self.apps.list_namespaced_deployment(
            self.cfg.namespace, label_selector=selector
        ).items:
            worker = deploy.metadata.labels.get("app.kubernetes.io/owner", "")
            if worker and worker not in live_workers:
                self.apps.delete_namespaced_deployment(deploy.metadata.name, self.cfg.namespace)
                log.info("gc Deployment %s (worker %s gone)", deploy.metadata.name, worker)
        for svc in self.core.list_namespaced_service(
            self.cfg.namespace, label_selector=selector
        ).items:
            worker = svc.metadata.labels.get("app.kubernetes.io/owner", "")
            if worker and worker not in live_workers:
                self.core.delete_namespaced_service(svc.metadata.name, self.cfg.namespace)
                log.info("gc Service %s", svc.metadata.name)
        for secret in self.core.list_namespaced_secret(
            self.cfg.namespace, label_selector=selector
        ).items:
            worker = secret.metadata.labels.get("app.kubernetes.io/owner", "")
            if worker and worker not in live_workers:
                self.core.delete_namespaced_secret(secret.metadata.name, self.cfg.namespace)
                log.info("gc Secret %s", secret.metadata.name)

    # ------------------------------------------------------------------
    # reconcile
    # ------------------------------------------------------------------

    def reconcile(self) -> None:
        workers = self.opencode_workers()
        self.garbage_collect(workers)
        for worker, worker_obj in sorted(workers.items()):
            try:
                self.reconcile_worker(worker, worker_obj)
            except Exception:
                log.exception("reconcile failed for worker %s", worker)

    def reconcile_worker(self, worker: str, worker_obj: dict[str, Any]) -> None:
        base_svc = f"opencode-{worker}-svc"
        sandbox_svc = f"opencode-{worker}-sandbox-svc"
        self.ensure_service(self.svc(worker, base_svc, f"opencode-{worker}", 4096))
        self.ensure_service(self.svc(worker, sandbox_svc, f"opencode-{worker}-sandbox", 4097))
        self.ensure_deployment(self.runtime_deployment(worker))

        team = self.team_for(worker)
        matrix_user = f"@{worker}:{self.cfg.matrix_server}"
        bridge_env = self.bridge_pod_env(worker)
        if bridge_env is None:
            log.info(
                "worker %s: bridge pod not found yet — deferring sandbox creds/team wiring",
                worker,
            )
            self.ensure_worker_env(worker, worker_obj)
            return
        access = bridge_env.get("AGENTTEAMS_FS_ACCESS_KEY", "")
        secret_key = bridge_env.get("AGENTTEAMS_FS_SECRET_KEY", "")
        if not access or not secret_key:
            log.warning("worker %s: bridge pod env lacks FS credentials; deferring", worker)
            return
        self.ensure_secret(
            f"opencode-sandbox-fs-{worker}",
            worker,
            {"accessKey": access, "secretKey": secret_key},
        )
        self.ensure_deployment(self.sandbox_deployment(worker, team, matrix_user))
        self.ensure_worker_env(worker, worker_obj)
        log.info(
            "worker %s reconciled (team=%s stack=%s)",
            worker,
            team or "(none)",
            base_svc,
        )

    def run(self) -> None:
        log.info(
            "opencode-stack-operator starting ns=%s opencode=%s sandbox=%s interval=%ss",
            self.cfg.namespace,
            self.cfg.opencode_image,
            self.cfg.sandbox_image,
            self.cfg.interval,
        )
        while True:
            try:
                self.reconcile()
            except Exception:
                log.exception("reconcile pass failed")
            time.sleep(self.cfg.interval)


def main() -> int:
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    StackOperator(OperatorConfig()).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
