"""Late runtime-wiring recovery: a bridge pod can start before its runtime
wiring is usable (historically the operator wrote Worker spec.env ~44s after
pod creation and the controller never rolls pods on spec.env changes). The
self-heal loop now has three channels, whichever is ready first wins:
A. re-pull the S3 bootstrap (runtime.yaml + matrix credentials landing late);
B. derived pod wiring (the main channel) — the svc name is a contract-level
   constant, so the base_url value is knowable up front; what arrives late is
   reachability, retried via the GET /session health probe;
C. poll GET /api/v1/workers/{self} runtimeEnv for legacy BRIDGE_RUNTIME_*
   keys written by an older operator. Everything rebuilds in-process — no pod
   recreation."""

import asyncio
import logging
import os
from types import SimpleNamespace

import pytest

from cimicode_bridge.app import BridgeApp
from cimicode_bridge.bootstrap import WorkerBootstrapConfig
from cimicode_bridge.config import load_config
from cimicode_bridge.runtime.cimicode_pod_adapter import CimicodePodAdapter


class _FakeGateway:
    def __init__(self, connected: bool):
        self.connected = connected
        self.user_id = "@w1:example.org"
        self.started = False

    async def start(self):
        self.started = True


def _app_with_config() -> BridgeApp:
    app = BridgeApp()
    app.config = load_config("config/bridge.example.yaml")
    return app


def _bootstrap_app(monkeypatch, runtime_env, poll_seconds=0.0):
    """Build a BridgeApp in the wedged state (no gateway) with a stubbed
    controller poll returning `runtime_env`."""
    app = _app_with_config()
    app.phase = "bootstrap"
    app.matrix_gateway = None
    app.recovery_task = None

    async def fake_fetch(worker, controller_url):
        return runtime_env

    monkeypatch.setattr(app, "_fetch_runtime_env", fake_fetch)
    app.RECOVERY_POLL_SECONDS = poll_seconds
    return app


def test_fetch_runtime_env_uses_controller_url_and_worker_env(monkeypatch):
    """The poll hits GET {controller}/api/v1/workers/{worker} with the
    worker's bearer token and returns the runtimeEnv mapping."""
    captured = {}

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            # The controller filters runtimeEnv server-side (BRIDGE_RUNTIME_
            # prefix only — see WorkerResponse.RuntimeEnv); the bridge trusts
            # that contract. "unrelated" at the top level must not leak in.
            return {
                "name": "w1",
                "unrelated": "must-not-leak",
                "runtimeEnv": {
                    "BRIDGE_RUNTIME_ADAPTER": "cimicode-pod",
                    "BRIDGE_RUNTIME_BASE_URL": "http://sandbox:4096",
                },
            }

    class _Client:
        def __init__(self, *args, **kwargs):
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def __getattr__(self, item):
            return self

        async def get(self, url, headers=None):
            captured["url"] = url
            captured["headers"] = headers
            return _Response()

    monkeypatch.setattr("cimicode_bridge.controller.client.httpx.AsyncClient", _Client)
    monkeypatch.setenv("AGENTTEAMS_AUTH_TOKEN", "tok-123")
    monkeypatch.delenv("AGENTTEAMS_AUTH_TOKEN_FILE", raising=False)

    app = BridgeApp()
    result = asyncio.run(app._fetch_runtime_env("w1", "http://controller:8080"))

    assert captured["url"] == "http://controller:8080/api/v1/workers/w1"
    assert captured["headers"]["Authorization"] == "Bearer tok-123"
    # Only the BRIDGE_RUNTIME_* subset the controller exposes comes back;
    # the response shape guarantees this, the str-coercion keeps it clean.
    assert result == {
        "BRIDGE_RUNTIME_ADAPTER": "cimicode-pod",
        "BRIDGE_RUNTIME_BASE_URL": "http://sandbox:4096",
    }


def test_fetch_runtime_env_returns_empty_without_token(monkeypatch):
    monkeypatch.delenv("AGENTTEAMS_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("AGENTTEAMS_AUTH_TOKEN_FILE", raising=False)

    app = BridgeApp()
    result = asyncio.run(app._fetch_runtime_env("w1", "http://controller:8080"))

    assert result == {}


def test_recovery_builds_gateway_and_reaches_listening(monkeypatch):
    """Adapter lands in runtimeEnv → config updated, gateway rebuilt,
    phase flips to listening, recovery loop returns."""
    app = _bootstrap_app(
        monkeypatch,
        {
            "BRIDGE_RUNTIME_ADAPTER": "cimicode-pod",
            "BRIDGE_RUNTIME_BASE_URL": "http://sandbox:4096",
        },
    )
    monkeypatch.setenv("AGENTTEAMS_WORKER_NAME", "w1")
    monkeypatch.setenv("AGENTTEAMS_CONTROLLER_URL", "http://controller:8080")
    monkeypatch.setenv("AGENTTEAMS_MATRIX_URL", "http://synapse:8008")
    app.matrix_access_token = "mtx-token"

    gateways = []

    def fake_build_gateway():
        gw = _FakeGateway(connected=True)
        gateways.append(gw)
        return gw

    monkeypatch.setattr(app, "_build_matrix_gateway", fake_build_gateway)

    asyncio.run(asyncio.wait_for(app._recover_late_runtime_wiring(), timeout=2))

    assert app.config.runtime.adapter == "cimicode-pod"
    assert app.config.runtime.base_url == "http://sandbox:4096"
    assert isinstance(app.runtime_client, CimicodePodAdapter)
    assert len(gateways) == 1
    assert gateways[0].started is True
    assert app.phase == "listening"
    assert app.ready is True
    assert app.matrix_connected is True
    # The whoami-resolved identity from the new gateway feeds the filter.
    assert app.mention_filter.user_id == "@w1:example.org"


def test_recovery_polls_until_adapter_appears(monkeypatch):
    """First polls return no adapter; a later poll carries it. The loop must
    keep polling (not give up) and recover once the wiring lands."""
    polls = {"n": 0}

    async def fake_fetch(worker, controller_url):
        polls["n"] += 1
        if polls["n"] < 3:
            return {}
        # 旧 operator 形态恒两键齐写（base_url 显式则无需健康探测）
        return {
            "BRIDGE_RUNTIME_ADAPTER": "cimicode-pod",
            "BRIDGE_RUNTIME_BASE_URL": "http://sandbox:4096",
        }

    app = _app_with_config()
    app.phase = "bootstrap"
    app.matrix_gateway = None
    monkeypatch.setattr(app, "_fetch_runtime_env", fake_fetch)
    app.RECOVERY_POLL_SECONDS = 0.0
    monkeypatch.setenv("AGENTTEAMS_WORKER_NAME", "w1")
    monkeypatch.setenv("AGENTTEAMS_CONTROLLER_URL", "http://controller:8080")

    gateways = []

    def fake_build_gateway():
        gw = _FakeGateway(connected=True)
        gateways.append(gw)
        return gw

    monkeypatch.setattr(app, "_build_matrix_gateway", fake_build_gateway)

    asyncio.run(asyncio.wait_for(app._recover_late_runtime_wiring(), timeout=2))

    assert polls["n"] == 3
    assert app.phase == "listening"


def test_recovery_skips_redundant_rebuilds_for_unchanged_adapter(monkeypatch):
    """Wiring present but gateway still cannot start (e.g. homeserver down):
    the loop must NOT rebuild the adapter on every tick — only when the
    adapter value actually changes."""
    app = _bootstrap_app(monkeypatch, {"BRIDGE_RUNTIME_ADAPTER": "cimicode-pod"})
    monkeypatch.setenv("AGENTTEAMS_WORKER_NAME", "w1")
    monkeypatch.setenv("AGENTTEAMS_CONTROLLER_URL", "http://controller:8080")
    # env 无 base_url → 推导路径每轮做健康探测；stub 掉避免真实网络调用
    monkeypatch.setattr(app, "_probe_runtime_health", lambda base_url: False)

    builds = {"n": 0}
    fetched = {"n": 0}

    async def stop_after_five_polls():
        while fetched["n"] < 5:
            await asyncio.sleep(0)
        raise asyncio.CancelledError()

    def fake_build_gateway():
        builds["n"] += 1
        return None  # gateway still cannot start

    monkeypatch.setattr(app, "_build_matrix_gateway", fake_build_gateway)

    async def scenario():
        task = asyncio.create_task(app._recover_late_runtime_wiring())
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=2)
        except asyncio.TimeoutError:
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def fake_fetch(worker, controller_url):
        fetched["n"] += 1
        return {"BRIDGE_RUNTIME_ADAPTER": "cimicode-pod"}

    monkeypatch.setattr(app, "_fetch_runtime_env", fake_fetch)

    asyncio.run(scenario())

    assert fetched["n"] >= 5
    assert builds["n"] == 1  # unchanged adapter → exactly one rebuild


def test_recovery_noop_without_worker_or_controller_env(monkeypatch):
    monkeypatch.delenv("AGENTTEAMS_WORKER_NAME", raising=False)
    monkeypatch.delenv("AGENTTEAMS_CONTROLLER_URL", raising=False)

    app = BridgeApp()
    app.matrix_gateway = None
    # Must return immediately without raising or polling.
    asyncio.run(asyncio.wait_for(app._recover_late_runtime_wiring(), timeout=2))
    assert app.phase == "bootstrap"


def test_start_background_spawns_recovery_task_when_gateway_missing():
    """The wedged startup shape must spawn the recovery task."""
    app = BridgeApp()
    app.matrix_gateway = None

    async def scenario():
        await app.start_background()
        try:
            assert app.phase == "bootstrap"
            assert app.ready is False
            assert app.recovery_task is not None
            assert not app.recovery_task.done()
        finally:
            app.recovery_task.cancel()
            await asyncio.gather(app.recovery_task, return_exceptions=True)

    asyncio.run(scenario())


def test_shutdown_cancels_recovery_task():
    app = BridgeApp()
    app.matrix_gateway = None

    async def scenario():
        await app.start_background()
        task = app.recovery_task
        await app.shutdown()
        assert task.cancelled() or task.done()

    asyncio.run(scenario())


def test_start_background_spawns_recovery_when_client_missing(monkeypatch):
    """Matrix 通了但 runtime client 未建（runtime pod 尚未健康）——探针位
    刻意不动（ready 必须保持 True，否则 k8s 重启 bridge pod 也换不来接线，
    竞态原地打转成崩溃循环）；自愈轮询接管 client 重建。"""
    app = _app_with_config()
    app.config.runtime.adapter = "cimicode-pod"
    app.matrix_gateway = _FakeGateway(connected=True)
    app.runtime_client = None

    async def scenario():
        await app.start_background()
        try:
            assert app.phase == "listening"
            assert app.ready is True
            assert app.recovery_task is not None
            assert not app.recovery_task.done()
        finally:
            app.recovery_task.cancel()
            await asyncio.gather(app.recovery_task, return_exceptions=True)

    asyncio.run(scenario())


def test_derived_channel_builds_client_when_runtime_becomes_healthy(monkeypatch):
    """推导主通道：operator 不再回写 env——bridge 从 svc 命名契约推导接线，
    健康探测通过即在进程内补齐 client；在跑的 gateway 绝不重建（否则双
    sync 长轮询双消费 timeline 事件）。"""
    app = _app_with_config()
    app.worker_files = WorkerBootstrapConfig(openclaw={}, runtime_yaml="member:\n  runtime: worker-bridge\n")
    app.runtime_client = None
    gateway = _FakeGateway(connected=True)
    app.matrix_gateway = gateway

    async def empty_fetch(worker, controller_url):
        return {}

    monkeypatch.setattr(app, "_fetch_runtime_env", empty_fetch)
    monkeypatch.setattr(app, "_probe_runtime_health", lambda base_url: True)
    monkeypatch.setenv("AGENTTEAMS_WORKER_NAME", "w1")
    monkeypatch.setenv("AGENTTEAMS_CONTROLLER_URL", "http://controller:8080")

    async def scenario():
        keepalive = asyncio.create_task(asyncio.sleep(30))  # 模拟在跑的 sync 循环
        app.matrix_task = keepalive
        try:
            await asyncio.wait_for(app._recover_late_runtime_wiring(), timeout=2)
        finally:
            keepalive.cancel()

    asyncio.run(scenario())

    assert isinstance(app.runtime_client, CimicodePodAdapter)
    assert app.config.runtime.base_url == "http://w1-cimicode-svc:4096"
    assert app.phase == "listening"
    assert app.matrix_gateway is gateway


def test_accepted_mention_drop_is_visible_and_kicks_recovery(monkeypatch, caplog):
    """裸 return 静默丢弃 accepted 消息是事故第三层根因：client 缺失时
    必须告警 + 立即唤醒自愈轮询（下一条 mention 不必再等 15s tick）。"""
    app = _app_with_config()
    app.matrix_gateway = _FakeGateway(connected=True)
    app.runtime_client = None

    async def empty_fetch(worker, controller_url):
        return {}

    monkeypatch.setattr(app, "_fetch_runtime_env", empty_fetch)
    monkeypatch.setenv("AGENTTEAMS_WORKER_NAME", "w1")
    monkeypatch.setenv("AGENTTEAMS_CONTROLLER_URL", "http://controller:8080")

    decision = SimpleNamespace(accepted=True, role="leader", reason="ok", mentions=["@w1:example.org"])
    monkeypatch.setattr(app.mention_filter, "evaluate", lambda *a, **k: decision)

    async def scenario():
        with caplog.at_level(logging.WARNING, logger="cimicode_bridge.app"):
            await app.handle_matrix_message("!room", "@leader:example.org", "$ev1", {"body": "@w1 do it"})
        task = app.recovery_task
        assert task is not None
        app.recovery_task = None
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert any("dropped" in record.message for record in caplog.records)
