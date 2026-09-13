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
                              Service     cimicode-<worker>-svc  (<port>)
                              Deployment  cimicode-<worker>
                          and point the Worker CR spec.env at it (the bridge
                          pod picks it up via self-heal polling):
                              BRIDGE_RUNTIME_ADAPTER=cimicode-pod
                              BRIDGE_RUNTIME_BASE_URL=http://cimicode-<w>-svc.<ns>.svc:<port>
    (empty)             → treated as cimicode-pod, mirroring the controller's
                          projection normalization (empty adapterMode with any
                          binding field → cimicode-pod).

No sandbox, no hostPath, no FS Secret: the cimicode runtime pod is a plain
stateless HTTP service. The bridge pod itself stays controller-managed.

CIMICODE_IMAGE is required (no default — fail loud per pass until the env is
supplied); CIMICODE_PORT defaults to 8080. Readiness probe is opt-in via
CIMICODE_PROBE_PATH because the cimicode pod's HTTP surface is not pinned by
this contract — set it to match the actual image during integration testing.

Deleting the worker (or changing runtime away from worker-bridge /
adapterMode to cimicode-stateless) garbage-collects the stack via the
managed-by label.

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
log = logging.getLogger("worker-bridge-operator")

GROUP = "agentteams.io"
VERSION = "v1beta1"
WORKERS_PLURAL = "workers"

MANAGED_BY = "worker-bridge-operator"
ENV_KEY_ADAPTER = "BRIDGE_RUNTIME_ADAPTER"
ENV_KEY_BASE_URL = "BRIDGE_RUNTIME_BASE_URL"

ADAPTER_STATELESS = "cimicode-stateless"
ADAPTER_POD = "cimicode-pod"
RUNTIME_WORKER_BRIDGE = "worker-bridge"


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


class OperatorConfig:
    def __init__(self) -> None:
        self.namespace = env("WATCH_NAMESPACE", "")
        # Required, no default: the cimicode runtime image is environment
        # specific (internal registry). Missing → per-pass error log, nothing
        # provisioned, instead of silently running a wrong/mocked image.
        self.cimicode_image = env("CIMICODE_IMAGE", "")
        self.cimicode_port = int(env("CIMICODE_PORT", "8080"))
        # Opt-in readiness probe: the cimicode HTTP surface is not pinned by
        # this contract. Empty → no probe (integration tuning decides).
        self.cimicode_probe_path = env("CIMICODE_PROBE_PATH", "")
        self.interval = int(env("RECONCILE_INTERVAL", "10"))
        # Effective runtime for workers whose spec.runtime is empty. Default
        # empty: this operator only acts on explicitly worker-bridge CRs.
        self.default_runtime = env("DEFAULT_RUNTIME", "")

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
                        name="http", port=self.cfg.cimicode_port, target_port=self.cfg.cimicode_port
                    )
                ],
            ),
        )

    def cimicode_svc_name(self, worker: str) -> str:
        return f"cimicode-{worker}-svc"

    def cimicode_deploy_name(self, worker: str) -> str:
        return f"cimicode-{worker}"

    def cimicode_deployment(self, worker: str) -> client.V1Deployment:
        name = self.cimicode_deploy_name(worker)
        container = client.V1Container(
            name="cimicode",
            image=self.cfg.cimicode_image,
            image_pull_policy="IfNotPresent",
            ports=[
                client.V1ContainerPort(name="http", container_port=self.cfg.cimicode_port)
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
                    spec=client.V1PodSpec(containers=[container]),
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
        live_probe_path = (
            lc.readiness_probe.http_get.path if lc.readiness_probe and lc.readiness_probe.http_get else ""
        )
        want_probe_path = (
            dc.readiness_probe.http_get.path if dc.readiness_probe and dc.readiness_probe.http_get else ""
        )
        return lc.image != dc.image or live_probe_path != want_probe_path

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

    def ensure_worker_env(self, worker: str, worker_obj: dict[str, Any]) -> None:
        base_url = (
            f"http://{self.cfg.cluster_dns(self.cimicode_svc_name(worker))}:{self.cfg.cimicode_port}"
        )
        wanted = {
            ENV_KEY_ADAPTER: ADAPTER_POD,
            ENV_KEY_BASE_URL: base_url,
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
        log.info("patched Worker %s spec.env -> cimicode pod %s", worker, base_url)

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
        self.ensure_service(self.svc(worker))
        self.ensure_deployment(self.cimicode_deployment(worker))
        self.ensure_worker_env(worker, worker_obj)
        log.info(
            "worker %s reconciled (mode=%s svc=%s)",
            worker,
            mode,
            self.cimicode_svc_name(worker),
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
