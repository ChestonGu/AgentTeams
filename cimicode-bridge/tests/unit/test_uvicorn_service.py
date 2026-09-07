import asyncio

from fastapi.testclient import TestClient

from cimicode_bridge.app import BridgeApp, create_app
from cimicode_bridge.bootstrap import WorkerBootstrapConfig
from cimicode_bridge.events import RuntimeEvent, RuntimeEventKind
from cimicode_bridge.runtime.client import HttpSseRuntime


class FakeRuntime:
    async def chat(self, **kwargs):
        self.request = kwargs
        return [
            RuntimeEvent(kind=RuntimeEventKind.TEXT_DELTA, text="done"),
            RuntimeEvent(kind=RuntimeEventKind.TURN_COMPLETED, text="done"),
        ]


class FakeMatrix:
    async def send_text(self, room_id, body):
        self.sent = (room_id, body)
        return "$reply"

    async def start_typing(self, room_id):
        self.typing_started = room_id

    async def stop_typing(self, room_id):
        self.typing_stopped = room_id


def test_healthz_and_readyz_endpoints():
    client = TestClient(create_app())

    health = client.get("/healthz")
    ready = client.get("/readyz")

    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert ready.status_code == 200
    assert ready.json()["ready"] is False


def test_status_endpoint_exposes_bridge_state():
    client = TestClient(create_app())

    status = client.get("/status")

    assert status.status_code == 200
    payload = status.json()
    assert payload["worker"] == "cimicode-bridge"
    assert payload["phase"] == "bootstrap"
    assert payload["matrix_connected"] is False


def test_bridge_message_endpoint_accepts_mentioned_message():
    client = TestClient(create_app())

    response = client.post(
        "/api/v1/bridge/handle-message",
        json={
            "event_id": "evt-1",
            "room_id": "!room-1",
            "sender": "alice",
            "body": "@leader please help me",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["accepted"] is True
    assert payload["forwarded"] is True
    assert payload["session_id"]


def test_matrix_event_calls_gateway_and_sends_reply():
    bridge = BridgeApp()
    bridge.start()
    bridge.config.runtime.session_id = "sess-1"
    bridge.config.runtime.sandbox_id = "sandbox-1"
    bridge.worker_files = WorkerBootstrapConfig(openclaw={}, agents_md="agent rules")
    bridge.runtime_client = FakeRuntime()
    bridge.matrix_gateway = FakeMatrix()

    asyncio.run(
        bridge.handle_matrix_message(
            "!room:matrix.local",
            "@leader:matrix.local",
            "$event-1",
            {"body": "@leader please inspect the page"},
        )
    )

    assert bridge.runtime_client.request["session_id"] == "sess-1"
    assert bridge.runtime_client.request["sandbox_id"] == "sandbox-1"
    assert bridge.runtime_client.request["turn_id"] == "$event-1"
    assert "[Current message - respond to this]" in bridge.runtime_client.request["user_message"]
    assert bridge.matrix_gateway.sent == ("!room:matrix.local", "done")
    assert bridge.matrix_gateway.typing_started == "!room:matrix.local"
    assert bridge.matrix_gateway.typing_stopped == "!room:matrix.local"


def test_opencode_turn_refetches_missing_runtime_yaml(monkeypatch):
    """Startup race: the bridge started before the controller pushed
    runtime/runtime.yaml. The turn must refetch the bootstrap before
    refusing — an unanswered delegation wedges the task in assigned
    state with no retry path."""
    bridge = BridgeApp()
    bridge.start()
    bridge.config.runtime.adapter = "opencode"
    bridge.worker_files = None
    recovered = WorkerBootstrapConfig(
        openclaw={}, runtime_yaml="member:\n  runtime: opencode\n"
    )
    loads = {"n": 0}

    class _StubBootstrap:
        bucket = "bkt"

        def load(self, *, retries=6, retry_interval_seconds=5):
            loads["n"] += 1
            return recovered

        def publish(self, name, text):
            return "agents/w1/" + name

    bridge.s3_bootstrap = _StubBootstrap()
    bridge.runtime_client = FakeRuntime()
    bridge.matrix_gateway = FakeMatrix()
    monkeypatch.setattr(
        "cimicode_bridge.app.build_agent_md_via_generator",
        lambda **kwargs: "# rendered agent md",
    )

    asyncio.run(
        bridge.handle_matrix_message(
            "!room:matrix.local",
            "@leader:matrix.local",
            "$evt-race",
            {"body": "@leader you are assigned task multimd-01"},
        )
    )

    assert loads["n"] == 1                    # refetch happened at turn time
    assert bridge.worker_files is recovered   # bootstrap cache replaced
    assert bridge.runtime_client.request["agent_md"] == "# rendered agent md"
    assert bridge.matrix_gateway.sent == ("!room:matrix.local", "done")
