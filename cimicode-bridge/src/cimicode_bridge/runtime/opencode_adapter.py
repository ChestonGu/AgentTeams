from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from cimicode_bridge.events import RuntimeEvent, RuntimeEventKind

logger = logging.getLogger(__name__)


class OpenCodeAdapter:
    """RuntimeAdapter over the opencode headless server (``opencode serve``).

    Protocol notes (stable opencode REST API, calibrated against 1.18.27):
      * sessions:  ``POST /session`` -> session object (top-level ``id``);
        ``GET /session`` -> list
      * messages:  ``POST /session/{id}/message`` body ``{"parts": [{"type":
        "text", "text": ...}]}`` — the call BLOCKS until the turn finishes
        (or the model errors out), so it gets its own long timeout;
        ``GET /session/{id}/message`` -> list of ``{info: {id, role, time:
        {created, completed?}, error?}, parts: [...]}``
      * completion signal: a finished assistant message has
        ``info.time.completed`` set; a failed turn carries ``info.error``
        (``error.data.message`` holds the upstream message, e.g. quota
        errors surfaced as retry loops). SSE exists but recent releases
        have reliability issues — we poll instead.
      * system prompt: opencode reads an ``AGENTS.md`` from the working
        directory (a volume shared with the sandbox pod); the generated
        agent.md is shipped there via the sandbox helper endpoint
        (``POST {helper_url}/agents-md``, body = raw markdown) before each
        turn.

    Session binding: the controller may pre-create a session id in
    openclaw.json (``bridge.runtime.sessionId``). When empty the adapter owns
    the session (creates on first turn, reuses afterwards, recreates on 404
    after a sandbox restart).
    """

    name = "opencode"

    def __init__(
        self,
        base_url: str,
        *,
        helper_url: str = "",
        timeout_seconds: int = 600,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.helper_url = helper_url.rstrip("/") if helper_url else ""
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self._session_id = ""
        self._client: httpx.AsyncClient | None = None

    def capabilities(self):  # pragma: no cover - trivial
        from cimicode_bridge.runtime.base import RuntimeCapabilities

        return RuntimeCapabilities(
            supports_session_destroy=True,
            supports_interrupt_event=False,
            supports_artifact=False,
            supports_streaming=False,
        )

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout_seconds)
        return self._client

    async def health(self) -> bool:
        try:
            response = await self._http().get(f"{self.base_url}/session")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def _push_agent_md(self, agent_md: str) -> None:
        if not self.helper_url:
            raise RuntimeError("opencode adapter requires runtime.helper_url (sandbox AGENTS.md helper)")
        logger.info(
            "opencode action: push agent.md to sandbox helper=%s bytes=%d",
            self.helper_url,
            len(agent_md.encode("utf-8")),
        )
        response = await self._http().post(
            f"{self.helper_url}/agents-md",
            content=agent_md.encode("utf-8"),
            headers={"Content-Type": "text/plain; charset=utf-8"},
        )
        response.raise_for_status()

    async def _ensure_session(self, session_id: str) -> str:
        if not session_id:
            session_id = self._session_id
        if session_id:
            response = await self._http().get(f"{self.base_url}/session/{session_id}")
            if response.status_code == 200:
                self._session_id = session_id
                logger.info("opencode action: reuse session id=%s", session_id)
                return session_id
            logger.warning("opencode session %s vanished; recreating", session_id)
        response = await self._http().post(f"{self.base_url}/session", json={})
        response.raise_for_status()
        created = response.json()
        session_id = str(
            created.get("id")
            or created.get("sessionID")
            or (created.get("info") or {}).get("id")
            or ""
        )
        if not session_id:
            raise RuntimeError(f"opencode session creation returned no id: {created!r}")
        self._session_id = session_id
        logger.info("opencode action: created session id=%s", session_id)
        return session_id

    async def _messages(self, session_id: str) -> list[dict[str, Any]]:
        response = await self._http().get(f"{self.base_url}/session/{session_id}/message")
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            return payload["data"]
        return payload if isinstance(payload, list) else []

    @staticmethod
    def _last_assistant_id(messages: list[dict[str, Any]]) -> str:
        for message in reversed(messages):
            info = message.get("info") or message
            if str(info.get("role")) == "assistant":
                return str(info.get("id") or "")
        return ""

    @staticmethod
    def _extract_text(message: dict[str, Any]) -> str:
        info = message.get("info") or message
        parts = info.get("parts") or message.get("parts") or []
        texts: list[str] = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text" and part.get("text"):
                texts.append(str(part["text"]))
        return "\n".join(texts)

    async def _poll_reply(self, session_id: str, baseline_id: str) -> RuntimeEvent:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_seconds
        while True:
            await asyncio.sleep(self.poll_interval_seconds)
            messages = await self._messages(session_id)
            for message in reversed(messages):
                info = message.get("info") or message
                if str(info.get("role")) != "assistant":
                    continue
                message_id = str(info.get("id") or "")
                if not message_id or message_id == baseline_id:
                    continue
                time_info = info.get("time") or {}
                if time_info.get("completed") is None:
                    continue
                error = info.get("error")
                if isinstance(error, dict):
                    data = error.get("data") or {}
                    detail = str(data.get("message") or error.get("name") or "opencode turn failed")
                    return RuntimeEvent(
                        kind=RuntimeEventKind.RUNTIME_ERROR,
                        text=f"opencode turn failed: {detail}",
                        data={"session_id": session_id, "message_id": message_id},
                    )
                text = self._extract_text(message)
                return RuntimeEvent(
                    kind=RuntimeEventKind.TURN_COMPLETED,
                    text=text,
                    data={"session_id": session_id, "message_id": message_id},
                )
            if loop.time() >= deadline:
                return RuntimeEvent(
                    kind=RuntimeEventKind.TURN_INTERRUPTED,
                    text="opencode turn timed out waiting for assistant completion",
                    data={"session_id": session_id},
                )

    async def chat(
        self,
        *,
        session_id: str,
        sandbox_id: str,
        turn_id: str,
        agent_md: str,
        history: list[dict[str, Any]],
        user_message: str,
    ) -> list[RuntimeEvent]:
        # history is already folded into user_message by the caller
        # (HistoryStore.build_context, contract §4 two-marker form);
        # sandbox binding is carried by base_url itself.
        del history, sandbox_id
        turn_started = time.monotonic()
        try:
            await self._push_agent_md(agent_md)
            resolved = await self._ensure_session(session_id)
            baseline = self._last_assistant_id(await self._messages(resolved))
            logger.info(
                "opencode action: post message session=%s turn=%s user_message=%r",
                resolved,
                turn_id,
                user_message[:300],
            )
            response = await self._http().post(
                f"{self.base_url}/session/{resolved}/message",
                json={"parts": [{"type": "text", "text": user_message}]},
                # POST blocks server-side until the whole turn finishes
                timeout=self.timeout_seconds + 30.0,
            )
            response.raise_for_status()
            completed = await self._poll_reply(resolved, baseline)
            logger.info(
                "opencode action: turn finished session=%s turn=%s outcome=%s elapsed=%.1fs reply=%r",
                resolved,
                turn_id,
                completed.kind.value,
                time.monotonic() - turn_started,
                (completed.text or "")[:300],
            )
            if completed.kind == RuntimeEventKind.TURN_COMPLETED:
                return [
                    RuntimeEvent(kind=RuntimeEventKind.TEXT_DELTA, text=completed.text),
                    completed,
                ]
            return [completed]
        except httpx.HTTPStatusError as exc:
            logger.error("opencode HTTP error: %s %s", exc.response.status_code, exc.response.text[:400])
            return [RuntimeEvent(kind=RuntimeEventKind.RUNTIME_ERROR, text=f"opencode HTTP {exc.response.status_code}")]
        except (httpx.HTTPError, RuntimeError) as exc:
            logger.error("opencode adapter failure: %s", exc)
            return [RuntimeEvent(kind=RuntimeEventKind.RUNTIME_ERROR, text=f"opencode adapter failure: {exc}")]
