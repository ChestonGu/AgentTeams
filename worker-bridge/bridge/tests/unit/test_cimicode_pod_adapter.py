"""CimicodePodAdapter：真实走 httpx + 本地回环假 opencode/helper JSON 服务器。

按 opencode 1.18.27 REST 契约（POST /session、POST /session/{id}/message
阻塞整 turn、GET /session/{id}/message 轮询 info.time.completed）回放，
覆盖：happy path / progress_texts / info.error / 轮询超时 / helper_url 缺失
fail-loud / 会话 404 自愈重建。
"""
from __future__ import annotations

import asyncio
import json

from cimicode_bridge.events import RuntimeEventKind
from cimicode_bridge.runtime.cimicode_pod_adapter import CimicodePodAdapter


class FakeCimicodePod:
    """有状态假服务：opencode REST + helper 端点同端口。

    脚本化行为：``scripted`` 描述 turn 完成后的 assistant 消息
    （text / error / progress 前置叙述）；``delay_polls`` 控制完成信号
    在第几次消息轮询后可见（模拟 turn 执行中的窗口期）。
    """

    def __init__(self, *, scripted: dict | None = None, delay_polls: int = 0):
        self.requests: list[tuple[str, str, str]] = []   # (method, path, body)
        self.sessions: dict[str, list[dict]] = {}        # 会话 id → 消息列表
        self.next_session_id = "ses_created"
        self.scripted = scripted or {"text": "final reply"}
        self.delay_polls = delay_polls
        self.message_polls = 0
        self.turn_posted = False
        self.injected = False                               # 本 turn 是否已注入完成信号
        self.vanished: set[str] = set()                  # 强制 404 的会话 id

    # -- 脚本辅助 ------------------------------------------------------
    def seed_session(self, session_id: str, messages: list[dict] | None = None) -> None:
        self.sessions[session_id] = list(messages or [])

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await reader.readline()
            method, path, _ = request_line.decode().split(" ", 2)
            headers: dict[str, str] = {}
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                key, _, value = line.decode().partition(":")
                headers[key.strip().lower()] = value.strip()
            length = int(headers.get("content-length", "0"))
            body = (await reader.readexactly(length)).decode("utf-8") if length else ""
            self.requests.append((method, path, body))

            status, payload = self._route(method, path, body)
            data = json.dumps(payload).encode("utf-8") if payload is not None else b"{}"
            writer.write(
                f"HTTP/1.1 {status} OK\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(data)}\r\n"
                f"Connection: close\r\n\r\n".encode()
                + data
            )
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, BrokenPipeError):
                pass

    def _route(self, method: str, path: str, body: str) -> tuple[int, object]:
        # helper 端点（生产在 pod 内 :4097，测试复用同端口）
        if method == "POST" and path == "/agents-md":
            return 200, {"bytes": len(body)}
        if method == "GET" and path == "/healthz":
            return 200, {"ok": True}
        # opencode REST
        if method == "POST" and path == "/session":
            session_id = self.next_session_id
            self.sessions[session_id] = []
            return 200, {"id": session_id}
        if method == "GET" and path == "/session":
            return 200, [{"id": sid} for sid in self.sessions]
        if method == "GET" and path.startswith("/session/") and len(path.split("/")) == 3:
            session_id = path.split("/")[2]
            if session_id in self.vanished or session_id not in self.sessions:
                return 404, {"error": "not found"}
            return 200, {"id": session_id}
        if method == "POST" and path.startswith("/session/") and path.endswith("/message"):
            session_id = path.split("/")[2]
            self.sessions[session_id].append(
                {"info": {"id": f"msg_user_{len(self.requests)}", "role": "user",
                          "time": {"created": 1, "completed": 1}},
                 "parts": [{"type": "text", "text": json.loads(body)["parts"][0]["text"]}]}
            )
            # 轮询计数只在 turn 提交后有效（baseline 读取不算窗口期）
            self.message_polls = 0
            self.turn_posted = True
            self.injected = False
            return 200, {}
        if method == "GET" and path.startswith("/session/") and path.endswith("/message"):
            session_id = path.split("/")[2]
            if self.turn_posted:
                self.message_polls += 1
            messages = list(self.sessions[session_id])
            # 注入条件用 per-turn 标志而非"会话无 assistant"——种子了历史
            # 回复的会话（回归测试）同样要在窗口期后出现本 turn 完成信号
            if self.turn_posted and self.message_polls > self.delay_polls and not self.injected:
                self.injected = True
                for index, progress in enumerate(self.scripted.get("progress", [])):
                    messages.append(self._assistant(f"msg_p{index}", progress))
                error = self.scripted.get("error")
                if error is not None:
                    messages.append(self._assistant("msg_e", "", error=error))
                else:
                    messages.append(self._assistant("msg_final", self.scripted.get("text", "")))
                self.sessions[session_id] = messages
            return 200, messages
        return 404, {"error": f"unhandled {method} {path}"}

    @staticmethod
    def _assistant(message_id: str, text: str, error: dict | None = None) -> dict:
        info: dict = {"id": message_id, "role": "assistant", "time": {"created": 1, "completed": 2}}
        if error is not None:
            info["error"] = error
        return {"info": info, "parts": [{"type": "text", "text": text}]}

    async def start(self) -> str:
        server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        self._server = server
        return f"http://127.0.0.1:{port}"

    async def stop(self) -> None:
        self._server.close()
        await self._server.wait_closed()


async def _run_chat(adapter: CimicodePodAdapter, **overrides):
    kwargs = dict(session_id="", sandbox_id="", turn_id="t1",
                  agent_md="# agent md", history=[], user_message="hi")
    kwargs.update(overrides)
    return await adapter.chat(**kwargs)


def _adapter(base_url: str, *, helper_url: str | None = None, **kwargs) -> CimicodePodAdapter:
    return CimicodePodAdapter(
        base_url,
        helper_url=helper_url if helper_url is not None else base_url,
        poll_interval_seconds=0.01,
        **kwargs,
    )


# ----------------------------------------------------------------------
# 用例
# ----------------------------------------------------------------------
async def _happy_path():
    fake = FakeCimicodePod(scripted={"text": "real llm answer"}, delay_polls=2)
    base_url = await fake.start()
    try:
        adapter = _adapter(base_url)
        events = await _run_chat(adapter)
        await adapter.close()
    finally:
        await fake.stop()

    kinds = [e.kind for e in events]
    assert kinds == [RuntimeEventKind.TEXT_DELTA, RuntimeEventKind.TURN_COMPLETED]
    assert events[0].text == "real llm answer"
    assert events[1].text == "real llm answer"
    # agent.md 原文推给 helper；消息以 opencode parts 形态提交
    push = next(r for r in fake.requests if r[0] == "POST" and r[1] == "/agents-md")
    assert push[2] == "# agent md"
    posted = next(r for r in fake.requests if r[0] == "POST" and r[1].endswith("/message"))
    assert json.loads(posted[2]) == {"parts": [{"type": "text", "text": "hi"}]}
    # 会话创建一次并复用为自持 id
    assert any(r[1] == "/session" for r in fake.requests if r[0] == "POST")
    assert adapter._session_id == "ses_created"


async def _progress_texts_surfaced():
    fake = FakeCimicodePod(
        scripted={"text": "final reply", "progress": ["checking tasks...", "running tests..."]},
        delay_polls=1,
    )
    base_url = await fake.start()
    try:
        adapter = _adapter(base_url)
        events = await _run_chat(adapter)
        await adapter.close()
    finally:
        await fake.stop()

    completed = next(e for e in events if e.kind == RuntimeEventKind.TURN_COMPLETED)
    assert completed.text == "final reply"
    assert completed.data["progress_texts"] == ["checking tasks...", "running tests..."]


async def _error_becomes_runtime_error():
    fake = FakeCimicodePod(
        scripted={"error": {"name": "UsageLimit", "data": {"message": "quota exceeded"}}},
    )
    base_url = await fake.start()
    try:
        adapter = _adapter(base_url)
        events = await _run_chat(adapter)
        await adapter.close()
    finally:
        await fake.stop()

    assert [e.kind for e in events] == [RuntimeEventKind.RUNTIME_ERROR]
    assert "quota exceeded" in events[0].text


async def _poll_deadline_interrupts():
    # scripted=None 且 delay_polls 极大：完成信号永不出现 → 轮询窗口耗尽
    fake = FakeCimicodePod(scripted={}, delay_polls=10**9)
    base_url = await fake.start()
    try:
        adapter = _adapter(base_url, timeout_seconds=1)
        events = await _run_chat(adapter)
        await adapter.close()
    finally:
        await fake.stop()

    assert [e.kind for e in events] == [RuntimeEventKind.TURN_INTERRUPTED]


async def _missing_helper_url_fails_loud():
    fake = FakeCimicodePod(scripted={"text": "never reached"})
    base_url = await fake.start()
    try:
        adapter = _adapter(base_url, helper_url="")
        events = await _run_chat(adapter)
        await adapter.close()
    finally:
        await fake.stop()

    assert [e.kind for e in events] == [RuntimeEventKind.RUNTIME_ERROR]
    assert "helper_url" in events[0].text
    # fail-loud 在推 agent.md 之前：不应出现任何 message 提交
    assert not any(r[1].endswith("/message") and r[0] == "POST" for r in fake.requests)


async def _vanished_session_recreated():
    fake = FakeCimicodePod(scripted={"text": "after recreate"})
    base_url = await fake.start()
    try:
        adapter = _adapter(base_url)
        adapter._session_id = "ses_before_restart"   # 模拟 pod 重建后会话目录丢失
        events = await _run_chat(adapter, session_id="ses_before_restart")
        await adapter.close()
    finally:
        await fake.stop()

    assert [e.kind for e in events][-1] == RuntimeEventKind.TURN_COMPLETED
    assert events[-1].text == "after recreate"
    # 旧 id 404 后走了重建（POST /session），并切到新 id
    assert adapter._session_id == "ses_created"
    assert any(r[0] == "POST" and r[1] == "/session" for r in fake.requests)


async def _progress_does_not_replay_history():
    """回归：同 session 的历史回复不得混进本 turn 的 progress_texts。

    旧实现只排除 baseline 那一条 id——baseline 之前的历史 assistant 也
    是已完成消息，会被全量收进 progress（105 实测 progress=0,1,2,3...
    随对话单调回放）。修复后按列表位置截断。
    """
    fake = FakeCimicodePod(
        scripted={"text": "本轮 final", "progress": ["本轮进度叙述"]},
        delay_polls=1,
    )
    fake.seed_session("ses_seed", [
        {"info": {"id": "msg_u_old", "role": "user", "time": {"created": 1, "completed": 1}},
         "parts": [{"type": "text", "text": "旧问题"}]},
        FakeCimicodePod._assistant("msg_a_hist", "旧回复（历史，不得回放）"),
        FakeCimicodePod._assistant("msg_a_baseline", "上一轮 final"),
    ])
    base_url = await fake.start()
    try:
        adapter = _adapter(base_url)
        events = await _run_chat(adapter, session_id="ses_seed")
        await adapter.close()
    finally:
        await fake.stop()

    completed = next(e for e in events if e.kind == RuntimeEventKind.TURN_COMPLETED)
    assert completed.text == "本轮 final"
    assert completed.data["progress_texts"] == ["本轮进度叙述"]


async def _health_true_when_session_list_ok():
    fake = FakeCimicodePod()
    fake.sessions["s1"] = []
    base_url = await fake.start()
    try:
        adapter = _adapter(base_url)
        assert await adapter.health() is True
        await adapter.close()
    finally:
        await fake.stop()


def test_happy_path_full_turn():
    asyncio.run(_happy_path())


def test_progress_texts_surfaced():
    asyncio.run(_progress_texts_surfaced())


def test_error_becomes_runtime_error():
    asyncio.run(_error_becomes_runtime_error())


def test_poll_deadline_interrupts():
    asyncio.run(_poll_deadline_interrupts())


def test_missing_helper_url_fails_loud():
    asyncio.run(_missing_helper_url_fails_loud())


def test_vanished_session_recreated():
    asyncio.run(_vanished_session_recreated())


def test_progress_does_not_replay_history():
    asyncio.run(_progress_does_not_replay_history())


def test_health_true_when_session_list_ok():
    asyncio.run(_health_true_when_session_list_ok())
