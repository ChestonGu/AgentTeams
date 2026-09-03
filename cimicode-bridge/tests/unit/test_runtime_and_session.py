from cimicode_bridge.events import RuntimeEvent, RuntimeEventKind
from cimicode_bridge.bootstrap import WorkerBootstrapConfig
from cimicode_bridge.matrix_client import MentionFilter, RoleResolver
from cimicode_bridge.probes import ProbeStatus, create_probe_status
from cimicode_bridge.runtime.adapters import CimicodeDialect
from cimicode_bridge.session import HistoryStore, SessionManager


def test_mention_filter_extracts_mentions():
    text = "hello @alice please check @bob and @team"
    mentions = MentionFilter.extract_mentions(text)
    assert mentions == ["alice", "bob", "team"]


def test_mention_filter_handles_matrix_structured_mentions():
    payload = {
        "m.mentions": {"user_ids": ["@leader:matrix.local"]},
        "formatted_body": "<a href=\"https://matrix.to/#/%40leader%3Amatrix.local\">leader</a>",
    }

    assert MentionFilter().should_handle("plain text", content=payload) is True
    assert MentionFilter().should_handle("plain chatter without mention") is False


def test_mention_filter_blocks_peer_workers():
    mention_filter = MentionFilter(
        user_id="@worker-a:matrix.local",
        role_resolver=RoleResolver(
            self_user_id="@worker-a:matrix.local",
            leader="@leader:matrix.local",
            workers={"@worker-a:matrix.local", "@worker-b:matrix.local"},
        ),
    )

    decision = mention_filter.evaluate(
        "@worker-a please help",
        "@worker-b:matrix.local",
    )

    assert decision.accepted is False
    assert decision.reason == "sender_not_allowed"
    assert decision.role == "worker"


def test_mention_filter_rejects_unknown_sender():
    mention_filter = MentionFilter(
        user_id="@worker-a:matrix.local",
        role_resolver=RoleResolver(
            self_user_id="@worker-a:matrix.local",
            leader="@leader:matrix.local",
        ),
    )

    decision = mention_filter.evaluate("@worker-a please help", "@stranger:matrix.local")

    assert decision.accepted is False
    assert decision.reason == "unknown_sender"


def test_history_store_prunes_old_entries():
    store = HistoryStore(capacity=2)
    store.append("u1", "first")
    store.append("u2", "second")
    store.append("u3", "third")

    assert [item["role"] for item in store.all()] == ["u2", "u3"]
    assert [item["content"] for item in store.all()] == ["second", "third"]


def test_history_store_builds_copaw_three_part_context():
    store = HistoryStore(capacity=2)
    store.append("alice", "先讨论登录页")

    context = store.build_context("@worker 请继续处理")

    assert "[Chat messages since your last reply - for context]" in context
    assert "alice: 先讨论登录页" in context
    assert "[Current message - respond to this]" in context
    assert context.endswith("@worker 请继续处理")


def test_s3_bootstrap_reads_matrix_token_from_openclaw_config():
    bootstrap = WorkerBootstrapConfig(
        openclaw={
            "channels": {
                "matrix": {"accessToken": "s3-token"},
            },
        },
    )

    assert bootstrap.matrix_access_token == "s3-token"


def test_s3_bootstrap_reads_precreated_gateway_session():
    bootstrap = WorkerBootstrapConfig(
        openclaw={
            "bridge": {
                "runtime": {
                    "sessionId": "sess-from-s3",
                    "sandboxId": "sandbox-from-s3",
                },
            },
        },
    )

    assert bootstrap.gateway_session_id == "sess-from-s3"
    assert bootstrap.gateway_sandbox_id == "sandbox-from-s3"


def test_cimicode_dialect_translates_runtime_event():
    raw = {"kind": "text_delta", "text": "hello", "seq": 7}
    events = CimicodeDialect().translate(raw)
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, RuntimeEvent)
    assert event.kind == RuntimeEventKind.TEXT_DELTA
    assert event.text == "hello"
    assert event.seq == 7


def test_cimicode_dialect_aggregates_parts_on_done():
    dialect = CimicodeDialect()
    dialect.translate({"event": "message.part.delta", "data": {"part_id": "p1", "delta": "hello"}})
    dialect.translate({"event": "message.part.updated", "data": {"part_id": "p2", "text": "world"}})
    dialect.translate({"event": "message.part.delta", "data": {"part_id": "p2", "delta": "!"}})

    completed = dialect.translate({"event": "done", "data": {}})[0]

    assert completed.kind == RuntimeEventKind.TURN_COMPLETED
    assert completed.text == "hello\nworld!"


def test_probe_status_contains_runtime_summary():
    status = create_probe_status("ready", {"runtime": "copaw", "matrix": "connected"})
    assert status.status == "ready"
    assert status.details["runtime"] == "copaw"
    assert status.details["matrix"] == "connected"


def test_session_manager_keeps_latest_turn():
    manager = SessionManager()
    manager.start_session("s1")
    manager.add_turn("s1", "turn-1", "first")
    manager.add_turn("s1", "turn-2", "second")

    assert manager.current_turn("s1") == "turn-2"
    assert manager.turn_history("s1")[-1]["content"] == "second"
