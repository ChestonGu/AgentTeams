from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from cimicode_bridge.config import RuntimeConfig
from cimicode_bridge.events import RuntimeEventKind
from cimicode_bridge.prompt import GenerateAgentMdError, build_agent_md_via_generator
from cimicode_bridge.runtime.client import HttpSseRuntime
from cimicode_bridge.runtime.opencode_adapter import OpenCodeAdapter
from cimicode_bridge.runtime.registry import build_runtime_adapter

REPO_ROOT = Path(__file__).resolve().parents[3]
GENERATOR = REPO_ROOT / "opencode" / "bridge" / "generate_agent_md.py"
# Real production-shaped fixture (desensitized) shared with the generator's
# own test suite — keeps this suite honest against fail-loud validation rules.
RUNTIME_YAML = (
    REPO_ROOT / "opencode" / "bridge" / "tests" / "fixtures" / "runtime.yaml"
).read_text(encoding="utf-8")


def _msg(mid: str, role: str, text: str = "", completed: bool = True, error: dict | None = None) -> dict:
    info: dict = {
        "id": mid,
        "role": role,
        "time": {"created": 1, "completed": 3 if completed else None},
    }
    if error is not None:
        info["error"] = error
    return {
        "info": info,
        "parts": [{"type": "text", "text": text}] if text else [],
    }


class FakeSandbox:
    """MockTransport-backed fake of the opencode server + AGENTS.md helper."""

    def __init__(self, *, script: list[list[dict]] | None = None) -> None:
        self.requests: list[tuple[str, str]] = []
        self.agent_md_received: list[str] = []
        self.messages: list[dict] = []
        self.next_session = 1
        self._script = script  # each poll round pops one message-list snapshot
        self._script_i = 0
        self.helper_down = False

        async def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append((request.method, request.url.path))
            path = request.url.path
            if path == "/agents-md":
                if self.helper_down:
                    return httpx.Response(500)
                self.agent_md_received.append(request.read().decode("utf-8"))
                return httpx.Response(200)
            if path == "/session" and request.method == "POST":
                sid = f"ses_{self.next_session}"
                self.next_session += 1
                return httpx.Response(200, json={"id": sid})
            if path == "/session":
                return httpx.Response(200, json=[])
            if path.startswith("/session/") and path.count("/") == 2:
                return httpx.Response(200, json={"id": path.rsplit("/", 1)[-1]})
            if path.startswith("/session/") and path.endswith("/message") and request.method == "POST":
                self.messages.append(_msg("m_user", "user"))
                return httpx.Response(200, json={"id": "m_user"})
            if path.startswith("/session/") and path.endswith("/message"):
                if self._script is not None:
                    snapshot = self._script[min(self._script_i, len(self._script) - 1)]
                    self._script_i += 1
                    return httpx.Response(200, json=snapshot)
                return httpx.Response(200, json=list(self.messages))
            if path.startswith("/session/") and "/event" in path:
                return httpx.Response(200)
            return httpx.Response(404)

        self.transport = httpx.MockTransport(handler)

    def attach(self, adapter: OpenCodeAdapter) -> None:
        adapter._client = httpx.AsyncClient(transport=self.transport, base_url=adapter.base_url)


def _adapter(**kwargs) -> OpenCodeAdapter:
    return OpenCodeAdapter(
        "http://sandbox:4096",
        helper_url="http://sandbox:4097",
        poll_interval_seconds=0,
        timeout_seconds=kwargs.pop("timeout_seconds", 30),
        **kwargs,
    )


class TestFactory:
    def test_cimicode_dispatch(self):
        cfg = RuntimeConfig(adapter="cimicode")
        assert isinstance(build_runtime_adapter(cfg), HttpSseRuntime)

    def test_opencode_dispatch(self):
        cfg = RuntimeConfig(adapter="opencode", base_url="http://sandbox:4096", helper_url="http://sandbox:4097")
        adapter = build_runtime_adapter(cfg)
        assert isinstance(adapter, OpenCodeAdapter)
        assert adapter.helper_url == "http://sandbox:4097"

    def test_helper_defaults_to_base_url(self):
        cfg = RuntimeConfig(adapter="opencode", base_url="http://sandbox:4096")
        assert build_runtime_adapter(cfg).helper_url == "http://sandbox:4096"

    def test_unknown_adapter_fails_loud(self):
        with pytest.raises(ValueError):
            build_runtime_adapter(RuntimeConfig(adapter="does-not-exist"))


class TestOpenCodeChat:
    def test_full_turn_polls_to_completion(self):
        fake = FakeSandbox(script=[
            [],  # baseline before POST
            [],  # poll 1: nothing yet
            [_msg("m_a", "assistant", text="", completed=False)],  # poll 2: in flight
            [_msg("m_a", "assistant", text="task done: ok", completed=True)],  # poll 3: done
        ])
        adapter = _adapter()
        fake.attach(adapter)

        events = asyncio.run(
            adapter.chat(
                session_id="",
                sandbox_id="",
                turn_id="evt-1",
                agent_md="# AGENTS",
                history=[],
                user_message="hello",
            )
        )

        kinds = [e.kind for e in events]
        assert RuntimeEventKind.TEXT_DELTA in kinds
        assert kinds[-1] == RuntimeEventKind.TURN_COMPLETED
        assert events[-1].text == "task done: ok"
        # agent.md reached the helper before the session was created
        assert fake.agent_md_received == ["# AGENTS"]
        assert fake.requests[0] == ("POST", "/agents-md")

    def test_session_is_reused_across_turns(self):
        fake = FakeSandbox(script=[
            [],  # baseline
            [_msg("m_1", "assistant", text="one", completed=True)],
            [],  # baseline turn 2 (assistant m_1 is there but snapshot pops this)
            [_msg("m_2", "assistant", text="two", completed=True)],
        ])
        adapter = _adapter()
        fake.attach(adapter)

        asyncio.run(adapter.chat(session_id="", sandbox_id="", turn_id="t1", agent_md="a", history=[], user_message="1"))
        asyncio.run(adapter.chat(session_id="", sandbox_id="", turn_id="t2", agent_md="a", history=[], user_message="2"))

        session_creates = [r for r in fake.requests if r == ("POST", "/session")]
        assert len(session_creates) == 1, "second turn must reuse the opencode session"

    def test_timeout_interrupts(self):
        fake = FakeSandbox(script=[[], [], []])
        adapter = _adapter(timeout_seconds=0)
        fake.attach(adapter)

        events = asyncio.run(
            adapter.chat(session_id="", sandbox_id="", turn_id="t", agent_md="a", history=[], user_message="x")
        )
        assert events[-1].kind == RuntimeEventKind.TURN_INTERRUPTED

    def test_turn_error_surfaces_as_runtime_error(self):
        # upstream failures (e.g. quota errors retried server-side) land on
        # info.error of a completed assistant message — must not read as a
        # successful empty turn
        fake = FakeSandbox(script=[
            [],
            [_msg("m_e", "assistant", completed=True, error={"name": "APIError", "data": {"message": "余额不足"}})],
        ])
        adapter = _adapter()
        fake.attach(adapter)

        events = asyncio.run(
            adapter.chat(session_id="", sandbox_id="", turn_id="t", agent_md="a", history=[], user_message="x")
        )
        assert events[-1].kind == RuntimeEventKind.RUNTIME_ERROR
        assert "余额不足" in events[-1].text

    def test_helper_failure_returns_runtime_error(self):
        fake = FakeSandbox()
        fake.helper_down = True
        adapter = _adapter()
        fake.attach(adapter)

        events = asyncio.run(
            adapter.chat(session_id="", sandbox_id="", turn_id="t", agent_md="a", history=[], user_message="x")
        )
        assert events[-1].kind == RuntimeEventKind.RUNTIME_ERROR

    def test_health(self):
        fake = FakeSandbox()
        adapter = _adapter()
        fake.attach(adapter)
        assert asyncio.run(adapter.health()) is True


class TestGeneratorPrompt:
    def test_real_generator_render(self):
        agent_md = build_agent_md_via_generator(
            runtime_yaml=RUNTIME_YAML,
            soul_md="Be terse.",
            generator_path=str(GENERATOR),
        )
        assert "t-8a3f2c-6b1d20c9f3e84a57d2b9c1f0a3e5d7" in agent_md
        assert "## Persona" in agent_md
        assert "{{" not in agent_md

    def test_empty_runtime_yaml_fail_loud(self):
        with pytest.raises(GenerateAgentMdError):
            build_agent_md_via_generator(runtime_yaml="", generator_path=str(GENERATOR))

    def test_invalid_runtime_yaml_fail_loud(self):
        with pytest.raises(GenerateAgentMdError):
            build_agent_md_via_generator(runtime_yaml="member: [broken", generator_path=str(GENERATOR))

    def test_missing_generator_fail_loud(self):
        with pytest.raises(GenerateAgentMdError):
            build_agent_md_via_generator(runtime_yaml=RUNTIME_YAML, generator_path="/nonexistent/g.py")
