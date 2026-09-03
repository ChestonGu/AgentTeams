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
