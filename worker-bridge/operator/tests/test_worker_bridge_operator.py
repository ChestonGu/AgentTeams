"""worker-bridge-operator 单测：期望对象构造 + drift/apply/reconcile 决策。

不连真实集群：StackOperator 的三个 API 属性替换为假实现，断言的都是
纯构造或"调用了哪个 API、带什么参数"。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kubernetes import client
from kubernetes.client.rest import ApiException

import worker_bridge_operator as wbo


def make_cfg(**overrides) -> wbo.OperatorConfig:
    cfg = wbo.OperatorConfig()
    cfg.namespace = "test-ns"
    cfg.cimicode_image = "agentteams/cimicode-runtime:v0.0.0-test"
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


class FakeCustom:
    """CustomObjectsApi 假实现：可编程的对象存储 + 调用记录。"""

    def __init__(self, workers=None, teams=None):
        self.workers = workers or {}
        self.teams = teams or []
        self.patches = []

    def list_namespaced_custom_object(self, group, version, ns, plural):
        if plural == "workers":
            return {"items": [{"metadata": {"name": n}, **obj} for n, obj in self.workers.items()]}
        return {"items": self.teams}

    def patch_namespaced_custom_object(self, group, version, ns, plural, name, body):
        self.patches.append((name, body))
        self.workers[name]["spec"]["env"] = body["spec"]["env"]


class FakeCore:
    def __init__(self, pods=None, secrets=None):
        self.pods = pods or {}
        self.secrets = secrets or {}
        self.deleted_secrets = []

    def read_namespaced_pod(self, name, ns):
        if name not in self.pods:
            raise ApiException(status=404)
        return self.pods[name]

    def read_namespaced_service(self, name, ns):
        raise ApiException(status=404)

    def create_namespaced_service(self, ns, body):
        self.created = body

    def read_namespaced_secret(self, name, ns):
        if name not in self.secrets:
            raise ApiException(status=404)
        return self.secrets[name]

    def create_namespaced_secret(self, ns, body):
        self.secrets[body.metadata.name] = _materialized_secret(body)

    def replace_namespaced_secret(self, name, ns, body):
        self.secrets[name] = _materialized_secret(body)

    def list_namespaced_secret(self, ns, label_selector=""):
        class _List:
            items = list(self.secrets.values())
        return _List()

    def list_namespaced_service(self, ns, label_selector=""):
        class _List:
            items = []
        return _List()

    def delete_namespaced_secret(self, name, ns):
        self.deleted_secrets.append(name)


class FakeApps:
    def __init__(self):
        self.deployments: dict[str, Any] = {}
        self.deleted = []

    def read_namespaced_deployment(self, name, ns):
        if name not in self.deployments:
            raise ApiException(status=404)
        return self.deployments[name]

    def create_namespaced_deployment(self, ns, body):
        self.deployments[body.metadata.name] = body

    def replace_namespaced_deployment(self, name, ns, body):
        self.deployments[name] = body

    def list_namespaced_deployment(self, ns, label_selector=""):
        class _List:
            items = list(self.deployments.values())
        return _List()

    def delete_namespaced_deployment(self, name, ns):
        self.deleted.append(name)


def make_operator(cfg=None, custom=None, core=None, apps=None) -> wbo.StackOperator:
    op = wbo.StackOperator(cfg or make_cfg())
    op.custom = custom or FakeCustom()
    op.core = core or FakeCore()
    op.apps = apps or FakeApps()
    return op


def make_bridge_pod(envs: dict[str, str]):
    return client.V1Pod(
        spec=client.V1PodSpec(
            containers=[client.V1Container(name="bridge", env=wbo.env_list(envs))]
        )
    )


def _materialized_secret(body) -> client.V1Secret:
    """模拟 API server 的 string_data → data(base64) 物化。"""
    import base64 as _b64

    return client.V1Secret(
        metadata=body.metadata,
        data={k: _b64.b64encode(v.encode()).decode() for k, v in (body.string_data or {}).items()},
    )


# ----------------------------------------------------------------------
# svc：双端口（runtime/helper）
# ----------------------------------------------------------------------
def test_svc_exposes_runtime_and_helper_ports():
    op = make_operator(make_cfg())
    svc = op.svc("w1")
    ports = {p.name: p.port for p in svc.spec.ports}
    assert ports == {"runtime": 4096, "helper": 4097}
    assert svc.metadata.name == "w1-cimicode-svc"
    assert svc.spec.selector == {"app": "w1-cimicode"}


# ----------------------------------------------------------------------
# deployment：工作 env + secretKeyRef + emptyDir
# ----------------------------------------------------------------------
def test_deployment_env_and_secret_ref():
    op = make_operator(make_cfg(fs_endpoint="http://minio:9000"))
    deploy = op.cimicode_deployment(
        "w1", team="t1", matrix_user="@w1:matrix.local", with_fs_secret=True
    )
    container = deploy.spec.template.spec.containers[0]
    plain = {e.name: e.value for e in container.env if e.value is not None}
    assert plain["AGENTTEAMS_FS_ROOT"] == "/workspace"
    assert plain["AGENTTEAMS_WORKER_NAME"] == "w1"
    assert plain["AGENTTEAMS_TEAM"] == "t1"
    assert plain["AGENTTEAMS_MATRIX_USER_ID"] == "@w1:matrix.local"
    assert plain["AGENTTEAMS_FS_ENDPOINT"] == "http://minio:9000"
    assert plain["SANDBOX_EXEC_URL"] == "http://127.0.0.1:4097"
    # AGENTTEAMS_RUNTIME 绝不出现（mc 同步只认 k8s/aliyun，本 pod 走 local 三元组）
    assert "AGENTTEAMS_RUNTIME" not in plain
    refs = {
        e.name: (e.value_from.secret_key_ref.name, e.value_from.secret_key_ref.key)
        for e in container.env
        if e.value_from and e.value_from.secret_key_ref
    }
    assert refs == {
        "AGENTTEAMS_FS_ACCESS_KEY": ("w1-cimicode-fs", "accessKey"),
        "AGENTTEAMS_FS_SECRET_KEY": ("w1-cimicode-fs", "secretKey"),
    }
    volumes = deploy.spec.template.spec.volumes
    assert len(volumes) == 1 and volumes[0].name == "workspace" and volumes[0].empty_dir is not None
    assert container.readiness_probe.http_get.path == "/session"


def test_deployment_without_fs_secret_has_no_refs():
    op = make_operator(make_cfg(fs_endpoint=""))
    deploy = op.cimicode_deployment("w1", with_fs_secret=False)
    container = deploy.spec.template.spec.containers[0]
    assert not [e for e in container.env if e.value_from]
    assert "AGENTTEAMS_FS_ENDPOINT" not in {e.name for e in container.env}


# ----------------------------------------------------------------------
# drift：env 变化要被识别
# ----------------------------------------------------------------------
def test_deployment_drift_reports_env_change():
    op = make_operator()
    desired = op.cimicode_deployment("w1", team="t1", matrix_user="@w1:m", with_fs_secret=True)
    drifted = op.cimicode_deployment("w1", team="t2", matrix_user="@w1:m", with_fs_secret=True)
    # live 与 desired 相同 → 无漂移
    assert op.deployment_drift(desired, desired) is False
    assert op.deployment_drift(drifted, desired) is True


def test_deployment_drift_reports_missing_secret_ref():
    op = make_operator(make_cfg(fs_endpoint="http://minio:9000"))
    with_ref = op.cimicode_deployment("w1", with_fs_secret=True)
    without_ref = op.cimicode_deployment("w1", with_fs_secret=False)
    assert op.deployment_drift(without_ref, with_ref) is True


# ----------------------------------------------------------------------
# adapter_mode：空 → pod
# ----------------------------------------------------------------------
def test_adapter_mode_empty_normalizes_to_pod():
    op = make_operator()
    assert op.adapter_mode({"spec": {}}) == "cimicode-pod"
    assert op.adapter_mode({"spec": {"adapterMode": "cimicode-stateless"}}) == "cimicode-stateless"


# ----------------------------------------------------------------------
# bridge_pod_env：双候选 pod 名
# ----------------------------------------------------------------------
def test_bridge_pod_env_dual_candidates():
    core = FakeCore(pods={
        # 只有旧命名（无 -bridge 后缀）在集群里
        "agentteams-worker-w1": make_bridge_pod({"AGENTTEAMS_FS_ACCESS_KEY": "ak"}),
    })
    op = make_operator(core=core)
    assert op.bridge_pod_env("w1") == {"AGENTTEAMS_FS_ACCESS_KEY": "ak"}

    core2 = FakeCore(pods={
        "agentteams-worker-w1-bridge": make_bridge_pod({"AGENTTEAMS_FS_ACCESS_KEY": "new"}),
        "agentteams-worker-w1": make_bridge_pod({"AGENTTEAMS_FS_ACCESS_KEY": "old"}),
    })
    op2 = make_operator(core=core2)
    assert op2.bridge_pod_env("w1") == {"AGENTTEAMS_FS_ACCESS_KEY": "new"}


def test_bridge_pod_env_none_when_both_missing():
    op = make_operator(core=FakeCore())
    assert op.bridge_pod_env("w1") is None


# ----------------------------------------------------------------------
# team_for / matrix_user_id
# ----------------------------------------------------------------------
def test_team_for_resolves_via_worker_members():
    teams = [
        {"metadata": {"name": "team-b"}, "spec": {"workerMembers": [{"name": "other"}]}},
        {"metadata": {"name": "team-a"}, "spec": {"workerMembers": [
            {"name": "leader", "role": "team_leader"}, {"name": "w1", "role": "worker"},
        ]}},
    ]
    op = make_operator(custom=FakeCustom(teams=teams))
    assert op.team_for("w1") == "team-a"
    assert op.team_for("nobody") == ""


def test_matrix_user_id_prefers_status():
    op = make_operator(make_cfg(matrix_server_name="synapse.local"))
    assert op.matrix_user_id("w1", {"status": {"matrixUserID": "@w1:real"}}) == "@w1:real"
    assert op.matrix_user_id("w1", {}) == "@w1:synapse.local"
    assert op.matrix_user_id("w1", {}) == "@w1:synapse.local"
    op_no_fallback = make_operator(make_cfg(matrix_server_name=""))
    assert op_no_fallback.matrix_user_id("w1", {}) == ""


# ----------------------------------------------------------------------
# ensure_worker_env：三键 patch
# ----------------------------------------------------------------------
def test_ensure_worker_env_patches_three_keys():
    custom = FakeCustom(workers={"w1": {"spec": {"env": {"OTHER": "keep"}}}})
    op = make_operator(custom=custom)
    op.ensure_worker_env("w1", custom.workers["w1"])
    name, body = custom.patches[0]
    assert name == "w1"
    envs = body["spec"]["env"]
    assert envs["OTHER"] == "keep"
    assert envs == {
        "OTHER": "keep",
        "BRIDGE_RUNTIME_ADAPTER": "cimicode-pod",
        "BRIDGE_RUNTIME_BASE_URL": "http://w1-cimicode-svc.test-ns.svc.cluster.local:4096",
        "BRIDGE_RUNTIME_HELPER_URL": "http://w1-cimicode-svc.test-ns.svc.cluster.local:4097",
    }


def test_ensure_worker_env_no_patch_when_current():
    custom = FakeCustom(workers={"w1": {"spec": {"env": {
        "BRIDGE_RUNTIME_ADAPTER": "cimicode-pod",
        "BRIDGE_RUNTIME_BASE_URL": "http://w1-cimicode-svc.test-ns.svc.cluster.local:4096",
        "BRIDGE_RUNTIME_HELPER_URL": "http://w1-cimicode-svc.test-ns.svc.cluster.local:4097",
    }}}})
    op = make_operator(custom=custom)
    op.ensure_worker_env("w1", custom.workers["w1"])
    assert custom.patches == []


# ----------------------------------------------------------------------
# ensure_secret：创建与轮转
# ----------------------------------------------------------------------
def test_ensure_secret_creates_then_rotates():
    import base64

    core = FakeCore()
    op = make_operator(core=core)
    op.ensure_secret("w1-cimicode-fs", "w1", {"accessKey": "ak", "secretKey": "sk"})
    assert "w1-cimicode-fs" in core.secrets
    # 相同数据不重写
    op.ensure_secret("w1-cimicode-fs", "w1", {"accessKey": "ak", "secretKey": "sk"})
    # 轮转
    op.ensure_secret("w1-cimicode-fs", "w1", {"accessKey": "ak2", "secretKey": "sk2"})
    live = core.secrets["w1-cimicode-fs"]
    decoded = {k: base64.b64decode(v).decode() for k, v in (live.data or {}).items()}
    assert decoded == {"accessKey": "ak2", "secretKey": "sk2"}


# ----------------------------------------------------------------------
# GC 覆盖 Secret
# ----------------------------------------------------------------------
def test_garbage_collect_removes_secret_for_deleted_worker():
    secret = client.V1Secret(
        metadata=client.V1ObjectMeta(
            name="w1-cimicode-fs",
            namespace="test-ns",
            labels=wbo.labels_for("w1"),
        ),
    )
    core = FakeCore(secrets={"w1-cimicode-fs": secret})
    op = make_operator(core=core)
    op.garbage_collect({})  # w1 已删
    assert core.deleted_secrets == ["w1-cimicode-fs"]


def test_garbage_collect_keeps_secret_for_live_pod_worker():
    secret = client.V1Secret(
        metadata=client.V1ObjectMeta(
            name="w1-cimicode-fs", namespace="test-ns", labels=wbo.labels_for("w1")
        ),
    )
    core = FakeCore(secrets={"w1-cimicode-fs": secret})
    op = make_operator(core=core)
    op.garbage_collect({"w1": {"spec": {"runtime": "worker-bridge", "adapterMode": "cimicode-pod"}}})
    assert core.deleted_secrets == []


# ----------------------------------------------------------------------
# reconcile_worker：bridge 未起推迟 / 全链路建齐
# ----------------------------------------------------------------------
def test_reconcile_defers_when_bridge_pod_missing():
    custom = FakeCustom(workers={"w1": {"spec": {"runtime": "worker-bridge"}}})
    apps = FakeApps()
    op = make_operator(custom=custom, core=FakeCore(), apps=apps)
    op.reconcile_worker("w1", custom.workers["w1"])
    assert apps.deployments == {}
    assert custom.patches == []


def test_reconcile_provisions_full_stack():
    custom = FakeCustom(
        workers={"w1": {"spec": {"runtime": "worker-bridge"},
                        "status": {"matrixUserID": "@w1:matrix.local"}}},
        teams=[{"metadata": {"name": "t1"}, "spec": {"workerMembers": [{"name": "w1"}]}}],
    )
    core = FakeCore(pods={
        "agentteams-worker-w1-bridge": make_bridge_pod({
            "AGENTTEAMS_FS_ACCESS_KEY": "ak", "AGENTTEAMS_FS_SECRET_KEY": "sk",
        }),
    })
    apps = FakeApps()
    op = make_operator(make_cfg(fs_endpoint="http://minio:9000"), custom=custom, core=core, apps=apps)
    op.reconcile_worker("w1", custom.workers["w1"])

    assert "w1-cimicode-fs" in core.secrets
    assert "w1-cimicode" in apps.deployments
    container = apps.deployments["w1-cimicode"].spec.template.spec.containers[0]
    plain = {e.name: e.value for e in container.env if e.value is not None}
    assert plain["AGENTTEAMS_TEAM"] == "t1"
    assert plain["AGENTTEAMS_MATRIX_USER_ID"] == "@w1:matrix.local"
    assert [e.name for e in container.env if e.value_from] == [
        "AGENTTEAMS_FS_ACCESS_KEY", "AGENTTEAMS_FS_SECRET_KEY",
    ]
    assert custom.patches and custom.patches[0][0] == "w1"


def test_reconcile_stateless_touches_nothing():
    custom = FakeCustom(workers={"w1": {"spec": {"runtime": "worker-bridge",
                                                 "adapterMode": "cimicode-stateless"}}})
    apps = FakeApps()
    core = FakeCore()
    op = make_operator(custom=custom, core=core, apps=apps)
    op.reconcile_worker("w1", custom.workers["w1"])
    assert apps.deployments == {}
    assert core.secrets == {}
    assert custom.patches == []
