from __future__ import annotations

import json
from typing import Any

import httpx

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
        """手写 SSE 解析：按空行分帧，data: 行拼接为 JSON。

        不依赖 httpx-sse（其 aconnect_sse 在容器内两种用法均异常）。
        诊断脚本已验证裸 httpx 逐行解析在本环境 100% 可收到 message/done。
        """
        headers = {"Accept": "text/event-stream"}
        if self.auth is not None:
            headers.update(await self.auth.attach(headers))

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            async with client.stream(
                method,
                f"{self.base_url}{path}",
                json=json_body,
                headers=headers,
            ) as response:
                response.raise_for_status()
                data_lines: list[str] = []
                async for raw_line in response.aiter_lines():
                    line = raw_line.rstrip("\r")
                    if line == "":
                        if data_lines:
                            data = "\n".join(data_lines)
                            try:
                                payload = json.loads(data)
                            except json.JSONDecodeError:
                                payload = {"raw": data}
                            yield {"event": "", "data": payload}
                            data_lines = []
                        continue
                    if line.startswith("data:"):
                        data_lines.append(line[5:].lstrip(" "))
                    elif line.startswith("event:") and data_lines == []:
                        # event 名在我们的协议里内嵌于 data.event，此处仅兼容透传
                        continue
                if data_lines:
                    data = "\n".join(data_lines)
                    try:
                        payload = json.loads(data)
                    except json.JSONDecodeError:
                        payload = {"raw": data}
                    yield {"event": "", "data": payload}

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
