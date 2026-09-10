import asyncio

from fastapi.testclient import TestClient

from cimicode_bridge.app import BridgeApp, create_app
from cimicode_bridge.bootstrap import WorkerBootstrapConfig
from cimicode_bridge.events import RuntimeEvent, RuntimeEventKind
from cimicode_bridge.runtime.cimicode_adapter import CimicodeAdapter


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


def test_turn_runner_aggregates_events():
    from cimicode_bridge.config import RuntimeConfig
    from cimicode_bridge.runtime.turn import TurnRunner

    class ErroringRuntime:
        async def chat(self, **kwargs):
            return [
                RuntimeEvent(kind=RuntimeEventKind.TEXT_DELTA, text="partial "),
                RuntimeEvent(kind=RuntimeEventKind.RUNTIME_ERROR, data={"code": "LLM_ERROR"}),
            ]

    runner = TurnRunner(config=RuntimeConfig(session_id="sess-1", sandbox_id="sandbox-1"))

    # 正常聚合：delta 追加 + turn_completed 覆盖为权威全文
    ok = runner.aggregate_reply(
        [
            RuntimeEvent(kind=RuntimeEventKind.TEXT_DELTA, text="Hel"),
            RuntimeEvent(kind=RuntimeEventKind.TEXT_DELTA, text="lo"),
            RuntimeEvent(kind=RuntimeEventKind.TURN_COMPLETED, text="Hello world"),
        ]
    )
    assert ok.failed is False
    assert ok.text == "Hello world"

    # 失败聚合：failed=True 且保留已聚合文本
    failed = runner.aggregate_reply(
        [RuntimeEvent(kind=RuntimeEventKind.RUNTIME_ERROR, data={"code": "LLM_ERROR"})]
    )
    assert failed.failed is True
    assert "LLM_ERROR" in failed.error

    # run_turn 透传 session/sandbox 并走同一聚合（用 fake client 验证请求体）
    class FakeClient:
        async def chat(self, **kwargs):
            self.request = kwargs
            return [RuntimeEvent(kind=RuntimeEventKind.TURN_COMPLETED, text="ok")]

    client = FakeClient()
    result = asyncio.run(
        runner.run_turn(
            client,
            worker_files=WorkerBootstrapConfig(openclaw={}, agents_md="agent rules"),
            room_id="!room:matrix.local",
            event_id="$event-1",
            user_message="hi",
        )
    )
    assert client.request["session_id"] == "sess-1"
    assert client.request["sandbox_id"] == "sandbox-1"
    assert client.request["turn_id"] == "$event-1"
    assert client.request["history"] == []
    assert result.text == "ok"

    # ErroringRuntime 路径：run_turn 返回 failed 结果
    error_result = asyncio.run(
        runner.run_turn(
            ErroringRuntime(),
            worker_files=None,
            room_id="!room:matrix.local",
            event_id="$event-2",
            user_message="hi",
        )
    )
    assert error_result.failed is True
    assert error_result.text == "partial "


def test_history_manager_room_scoping():
    from cimicode_bridge.session import CURRENT_MESSAGE_MARKER, HistoryManager

    manager = HistoryManager(capacity=10)

    # 两个 room 的 buffer 互不串扰
    manager.record_ambient("!room-a", "alice", "message in a", event_id="$a1")
    manager.record_ambient("!room-b", "bob", "message in b", event_id="$b1")

    context_a = manager.build_context("!room-a", "carol", "trigger")
    context_b = manager.build_context("!room-b", "dave", "trigger")
    assert "message in a" in context_a and "message in b" not in context_a
    assert "message in b" in context_b and "message in a" not in context_b

    # clear 后 build_context 只剩当前消息段（无历史标记）
    manager.clear("!room-a")
    context_after = manager.build_context("!room-a", "carol", "trigger")
    assert "message in a" not in context_after
    assert context_after.startswith(CURRENT_MESSAGE_MARKER)
