from __future__ import annotations

from typing import Any

import httpx
from httpx_sse import aconnect_sse

from cimicode_bridge.events import RuntimeEvent, RuntimeEventKind
from cimicode_bridge.runtime.adapters import CimicodeDialect


class HttpSseRuntime:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: int = 600,
        auth: Any | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.auth = auth

    async def request_json(self, method: str, path: str, *, json_body: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {}
        if self.auth is not None:
            headers = await self.auth.attach(headers)

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.request(method=method, url=f"{self.base_url}{path}", json=json_body, headers=headers)
            response.raise_for_status()
            return response.json() if response.content else {}

    async def stream_sse(self, method: str, path: str, *, json_body: dict[str, Any] | None = None):
        headers = {}
        if self.auth is not None:
            headers = await self.auth.attach(headers)

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            async with aconnect_sse(
                client,
                method,
                f"{self.base_url}{path}",
                json=json_body,
                headers=headers,
            ) as event_source:
                event_source.response.raise_for_status()
                async for event in event_source.aiter_sse():
                    if event.data:
                        yield {
                            "event": event.event,
                            "data": event.json(),
                        }

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
        path = "/v1/gateway/session/chat"
        events: list[RuntimeEvent] = []
        async for line in self.stream_sse(
            "POST",
            path,
            json_body={
                "sessionId": session_id,
                "sandboxId": sandbox_id,
                "turnId": turn_id,
                "agentMd": agent_md,
                "history": history,
                "userMessage": user_message,
            },
        ):
            events.extend(CimicodeDialect().translate(line))
        if not any(event.kind == RuntimeEventKind.TURN_COMPLETED for event in events):
            events.append(RuntimeEvent(kind=RuntimeEventKind.TURN_INTERRUPTED, text="Gateway stream ended before done"))
        return events

    async def submit_turn(self, *, session_id: str, turn_id: str, payload: dict[str, Any]) -> list[RuntimeEvent]:
        """Backward-compatible wrapper for the gateway chat call."""
        return await self.chat(
            session_id=session_id,
            sandbox_id=str(payload.get("sandboxId", payload.get("sandbox_id", ""))),
            turn_id=turn_id,
            agent_md=str(payload.get("agentMd", payload.get("agent_md", ""))),
            history=list(payload.get("history", [])),
            user_message=str(payload.get("userMessage", payload.get("user_message", ""))),
        )
