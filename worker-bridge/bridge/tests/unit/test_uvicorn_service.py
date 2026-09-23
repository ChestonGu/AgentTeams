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


def test_matrix_event_calls_gateway_and_sends_reply(monkeypatch):
    # worker-bridge 唯一 agent.md 路径 = v2.4 生成器（runtime.yaml 非空才放行
    # turn）；测试只验消息链路，生成器本身用 stub 替掉。
    monkeypatch.setattr(
        "cimicode_bridge.app.build_agent_md_via_generator",
        lambda **kwargs: "# generated agent.md",
    )
    bridge = BridgeApp()
    bridge.start()
    # 参数走 runtime.yaml（新约定）：绑定三件套全部由 bridge.runtimeParameter 袋供给
    bridge.worker_files = WorkerBootstrapConfig(
        openclaw={},
        runtime_yaml=(
            "member:\n  runtime: worker-bridge\n"
            "bridge:\n  adapterMode: cimicode-stateless\n"
            "  runtimeParameter:\n"
            "    sessionId: sess-1\n"
            "    sandboxId: sandbox-1\n"
            "    eid: emp-001\n"
        ),
    )
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
    assert bridge.runtime_client.request["agent_md"] == "# generated agent.md"
    assert "[Current message - respond to this]" in bridge.runtime_client.request["user_message"]
    assert bridge.matrix_gateway.sent == ("!room:matrix.local", "done")
    assert bridge.matrix_gateway.typing_started == "!room:matrix.local"
    assert bridge.matrix_gateway.typing_stopped == "!room:matrix.local"



def test_runtime_yaml_binding_rotation_applies_per_turn(monkeypatch):
    """runtime.yaml 新约定：袋内绑定（eid）轮转后，下一 turn 即生效。

    每 turn 重应用 _apply_bridge_section；无 eid/base_url 属性的 fake client
    不触发漂移重建（duck-typing 守卫），仍是原对象。
    """
    from cimicode_bridge.app import BridgeApp
    from cimicode_bridge.bootstrap import WorkerBootstrapConfig

    def make_yaml(eid: str) -> str:
        return (
            "member:\n"
            "  runtime: worker-bridge\n"
            "bridge:\n"
            "  adapterMode: cimicode-stateless\n"
            "  runtimeParameter:\n"
            f"    sessionId: sess-1\n"
            f"    sandboxId: sbx-1\n"
            f"    eid: {eid}\n"
        )

    app = BridgeApp(config_path="nonexistent.yaml")
    monkeypatch.setattr(
        "cimicode_bridge.app.build_agent_md_via_generator",
        lambda **kwargs: "# generated agent.md",
    )
    app.start()
    app.config.runtime.adapter = "cimicode-stateless"
    app.worker_files = WorkerBootstrapConfig(openclaw={}, runtime_yaml=make_yaml("user-1"))
    app._apply_bridge_section(app.worker_files)
    fake = FakeRuntime()
    app.runtime_client = fake
    app.matrix_gateway = FakeMatrix()

    asyncio.run(app.handle_matrix_message(
        "!room:matrix.local", "@leader:matrix.local", "$event-1",
        {"body": "@leader hi"},
    ))
    assert app.config.runtime.eid == "user-1"

    # 轮转 eid（runtime.yaml generation+1）：下一 turn 生效，client 不重建
    app.worker_files = WorkerBootstrapConfig(openclaw={}, runtime_yaml=make_yaml("user-2-rotated"))
    asyncio.run(app.handle_matrix_message(
        "!room:matrix.local", "@leader:matrix.local", "$event-2",
        {"body": "@leader hi again"},
    ))
    assert app.config.runtime.eid == "user-2-rotated"
    assert app.runtime_client is fake  # duck-typing 守卫：fake 无 eid 属性不触发重建

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
