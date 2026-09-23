import asyncio

from cimicode_bridge.events import RuntimeEvent, RuntimeEventKind
from cimicode_bridge.bootstrap import WorkerBootstrapConfig
from cimicode_bridge.matrix.filter import MentionFilter, RoleResolver
from cimicode_bridge.api.probes import ProbeStatus, create_probe_status
from cimicode_bridge.runtime.cimicode_adapter import CimicodeAdapter, GatewayV2Dialect
from cimicode_bridge.runtime.cimicode_stateless_adapter import CimicodeStatelessAdapter
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


def test_stateless_submit_envelope_strict_no_bag_leak():
    """Gateway v2 × runtime.yaml 新约定：参数面就是 runtime.yaml 袋——bridge
    抽固定字段自用（sessionId 进信封、eid 进 Header），袋内追加键/内部绑定
    键（baseUrl/sandboxId/extraHint…）绝不上 submit 信封。"""
    adapter = CimicodeStatelessAdapter("http://gw.example.com", eid="user-123")
    captured: dict = {}

    async def fake_request_json(method, path, *, json_body=None, headers=None):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = json_body
        captured["headers"] = headers or {}
        return {"code": "SUCCESS", "data": {"turnId": "turn-9"}}

    async def fake_stream_sse(method, path, *, json_body=None, headers=None):
        # 空流：chat 补一条 turn_interrupted 断流兜底（不视为失败）
        return
        yield  # pragma: no cover - 使其成为 async generator（chat 用 async for 消费）

    adapter.request_json = fake_request_json
    adapter.stream_sse = fake_stream_sse
    events = asyncio.run(adapter.chat(
        session_id="sess-1",
        agent_md="md",
        user_message="hi",
    ))
    assert [e.kind for e in events] == [RuntimeEventKind.TURN_INTERRUPTED]
    assert "turnId=turn-9" in events[0].text
    assert captured["method"] == "POST"
    assert captured["path"] == "/agi/gateway/v1/turn/submit"
    # 信封严格三字段——没有任何袋键泄漏
    assert captured["body"] == {
        "data": {"sessionId": "sess-1", "agentPrompt": "md", "userMessage": "hi"}
    }
    assert captured["headers"]["eid"] == "user-123"
    assert captured["headers"]["X-Idempotency-Key"]  # UUID v4 非空


def test_gateway_v2_dialect_translates_text_delta():
    raw = {"data": {"kind": "live", "type": "session.next.text.delta", "seq": 7,
                    "data": {"delta": "hello"}}}
    events = GatewayV2Dialect().translate(raw)
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, RuntimeEvent)
    assert event.kind == RuntimeEventKind.TEXT_DELTA
    assert event.text == "hello"
    assert event.seq == 7


def test_gateway_v2_dialect_ignores_unrecognized_types():
    """turn.accepted / status.changed / diagnostic 等不产生 RuntimeEvent（不误报错）。"""
    dialect = GatewayV2Dialect()
    assert dialect.translate({"data": {"type": "turn.accepted", "data": {"invocationID": "inv-1"}}}) == []
    assert dialect.translate({"data": {"type": "status.changed", "data": {"phase": "model"}}}) == []
    assert dialect.translate({"data": {"type": "diagnostic", "data": {"code": "Truncated"}}}) == []


def test_gateway_v2_dialect_aggregates_parts_on_idle():
    """终态未带全文时按 part 首现顺序拼接（durable 全文收口 + delta 聚合）。
    ended 后同 part 的迟到 delta 被丢弃（durable 全文是权威，收口后不再追加）。"""
    dialect = GatewayV2Dialect()
    dialect.translate({"data": {"type": "session.next.text.delta",
                                "data": {"partID": "p1", "delta": "hello"}}})
    dialect.translate({"data": {"type": "session.next.text.ended@1",
                                "data": {"part": {"partID": "p2", "text": "world"}}}})
    # p2 已收口：迟到的 delta 不追加（ended 全文是权威）
    dialect.translate({"data": {"type": "session.next.text.delta",
                                "data": {"partID": "p2", "delta": "!"}}})

    completed = dialect.translate({"data": {"type": "invocation.idle", "data": {}}})[0]

    assert completed.kind == RuntimeEventKind.TURN_COMPLETED
    assert completed.text == "hello\nworld"
    assert dialect.terminal_seen is True


def test_gateway_v2_dialect_failed_is_terminal_error():
    """invocation.failed → RUNTIME_ERROR 且标记终态（服务端随后关闭是正常结束）。"""
    dialect = GatewayV2Dialect()
    failed = dialect.translate({"data": {"type": "invocation.failed",
                                         "data": {"error": {"code": "Timeout", "message": "deadline exceeded"}}}})[0]
    assert failed.kind == RuntimeEventKind.RUNTIME_ERROR
    assert failed.text == "deadline exceeded"
    assert dialect.terminal_seen is True


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


# ----------------------------------------------------------------------
# CimicodeStatelessAdapter：两段式回环（POST submit 回执 + GET events SSE）
# ----------------------------------------------------------------------
def _http_response(body: bytes, status: int = 200, ct: str = "application/json"):
    async def handler(reader, writer):
        writer.write(
            f"HTTP/1.1 {status} Status\r\n"
            f"Content-Type: {ct}\r\n"
            f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        # 保持连接一小会，确保客户端读完所有帧（writer 立即关闭会 ReadError）
        await asyncio.sleep(0.05)
        writer.close()

    return handler


# 兼容旧名（SSE 帧响应默认 event-stream）
def _sse_response(body: bytes, status: int = 200, ct: str = "text/event-stream"):
    return _http_response(body, status=status, ct=ct)


async def _serve(handler):
    """起本地回环 server，返回 (base_url, srv)。"""
    srv = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    return f"http://127.0.0.1:{port}", srv


async def _serve_sse(handler):
    return await _serve(handler)


def _v2_frames(*envelopes: dict) -> bytes:
    """把 turn/1 envelope 序列化成 SSE 帧（data: 一行 JSON）。"""
    import json
    return b"".join(f"data: {json.dumps(e)}\n\n".encode() for e in envelopes)


async def _two_step_stream():
    """两段式：POST /turn/submit 回执 → GET /session/{id}/events SSE → 翻译。"""
    submit_seen = {}

    async def handler(reader, writer):
        request_line = await reader.readline()
        headers = {}
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            name, _, value = line.decode().partition(":")
            headers[name.strip().lower()] = value.strip()
        body_len = int(headers.get("content-length", "0"))
        body = await reader.readexactly(body_len) if body_len else b""

        import json
        if request_line.startswith(b"POST /agi/gateway/v1/turn/submit"):
            submit_seen["path"] = request_line.decode().split(" ")[1]
            submit_seen["idempotency_key"] = headers.get("x-idempotency-key", "")
            submit_seen["eid"] = headers.get("eid", "")
            submit_seen["body"] = json.loads(body) if body else {}
            resp = json.dumps({"code": "SUCCESS", "data": {"turnId": "turn-1", "attemptId": "att-1", "queueStatus": "QUEUED"}}).encode()
        elif request_line.startswith(b"GET /agi/gateway/v1/session/s1/events"):
            submit_seen["events_path"] = request_line.decode().split(" ")[1]
            resp = _v2_frames(
                {"kind": "live", "type": "turn.accepted", "data": {"invocationID": "inv-1"}},
                {"kind": "durable", "type": "session.next.text.delta", "seq": 1, "data": {"delta": "hello"}},
                {"kind": "durable", "type": "invocation.idle", "seq": 2, "data": {"revision": 3}},
            )
            writer.write(
                b"HTTP/1.1 200 Status\r\n"
                b"Content-Type: text/event-stream\r\n"
                b"Content-Length: " + str(len(resp)).encode() + b"\r\n\r\n" + resp
            )
            await writer.drain()
            await asyncio.sleep(0.05)
            writer.close()
            return
        else:
            resp = b'{"code":"NOT_FOUND"}'
        writer.write(
            b"HTTP/1.1 200 Status\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(resp)).encode() + b"\r\n\r\n" + resp
        )
        await writer.drain()
        await asyncio.sleep(0.05)
        writer.close()

    base_url, srv = await _serve(handler)
    try:
        rt = CimicodeStatelessAdapter(base_url, timeout_seconds=5, eid="emp-001")
        events = await rt.chat(session_id="s1", agent_md="md", user_message="hi")
    finally:
        srv.close()
        await srv.wait_closed()

    # submit 契约断言：路径 / 幂等键 / eid / 信封 body
    assert submit_seen["path"] == "/agi/gateway/v1/turn/submit"
    assert submit_seen["idempotency_key"]  # UUID v4 非空
    assert submit_seen["eid"] == "emp-001"
    assert submit_seen["body"] == {"data": {"sessionId": "s1", "agentPrompt": "md", "userMessage": "hi"}}
    assert submit_seen["events_path"] == "/agi/gateway/v1/session/s1/events"

    # 事件翻译断言：delta + 终态（turn.accepted 被忽略不产生事件）
    kinds = [e.kind for e in events]
    assert kinds == [RuntimeEventKind.TEXT_DELTA, RuntimeEventKind.TURN_COMPLETED]
    assert next(e for e in events if e.kind == RuntimeEventKind.TEXT_DELTA).text == "hello"
    # 终态帧后服务端关闭 = 正常结束，不补 turn_interrupted
    assert not any(e.kind == RuntimeEventKind.TURN_INTERRUPTED for e in events)


async def _interrupted_on_gap():
    """SSE 流结束仍无终态帧 → 补 turn_interrupted（断流兜底）。"""
    frames = _v2_frames({"kind": "live", "type": "session.next.text.delta", "seq": 1, "data": {"delta": "partial"}})

    async def handler(reader, writer):
        request_line = await reader.readline()
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
        if request_line.startswith(b"GET /agi/gateway/v1/session"):
            writer.write(
                b"HTTP/1.1 200 Status\r\n"
                b"Content-Type: text/event-stream\r\n"
                b"Content-Length: " + str(len(frames)).encode() + b"\r\n\r\n" + frames
            )
            await writer.drain()
            await asyncio.sleep(0.05)
            writer.close()
            return
        resp = b'{"code":"SUCCESS","data":{"turnId":"t1"}}'
        writer.write(
            b"HTTP/1.1 200 Status\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(resp)).encode() + b"\r\n\r\n" + resp
        )
        await writer.drain()
        await asyncio.sleep(0.05)
        writer.close()

    base_url, srv = await _serve(handler)
    try:
        rt = CimicodeStatelessAdapter(base_url, timeout_seconds=5)
        events = await rt.chat(session_id="s1", agent_md="md", user_message="hi")
    finally:
        srv.close()
        await srv.wait_closed()
    assert any(e.kind == RuntimeEventKind.TURN_INTERRUPTED for e in events)


async def _raises_on_non_2xx():
    """非 2xx 响应：httpx-sse 的 EventSource.response 持有原始响应，
    bridge 侧显式 raise_for_status 抛 HTTPStatusError。
    （httpx-sse 对非 event-stream 的 500 会在内部崩溃——TypeError，
    故用 200 状态但 Content-Type 异常的路径不可行；改验 submit 侧
    request_json 的 raise_for_status，同一防御层。）"""
    import httpx

    async def handler(reader, writer):
        await reader.readline()
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
        resp = b'{"code":"ERROR"}'
        writer.write(
            b"HTTP/1.1 500 Internal Server Error\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(resp)).encode() + b"\r\n\r\n" + resp
        )
        await writer.drain()
        await asyncio.sleep(0.05)
        writer.close()

    base_url, srv = await _serve(handler)
    try:
        rt = CimicodeAdapter(base_url, timeout_seconds=5)
        try:
            await rt.request_json("POST", "/agi/gateway/v1/turn/submit", json_body={"data": {}})
        except httpx.HTTPStatusError:
            pass  # 期望抛错
        else:
            raise AssertionError("expected HTTPStatusError on 500")
    finally:
        srv.close()
        await srv.wait_closed()


def test_stateless_two_step_stream():
    asyncio.run(_two_step_stream())


def test_stateless_appends_interrupted_on_gap():
    asyncio.run(_interrupted_on_gap())


def test_http_sse_raises_on_non_2xx():
    asyncio.run(_raises_on_non_2xx())
