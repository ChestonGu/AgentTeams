"""gateway HTTP + SSE 客户端引擎（标准 httpx-sse）。"""
from __future__ import annotations

import json
from typing import Any

import httpx
from httpx_sse import aconnect_sse

from cimicode_bridge.events import RuntimeEvent, RuntimeEventKind
from cimicode_bridge.runtime.adapters import CimicodeDialect


class HttpSseRuntime:
    """gateway 通用客户端：JSON 请求 + SSE 流消费（所有 adapter 共用）。"""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: int = 600,
        auth: Any | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds  # 流读超时 = turn 超时
        self.auth = auth                        # AuthProvider（当前 None）

    async def request_json(self, method: str, path: str, *, json_body: dict[str, Any] | None = None) -> dict[str, Any]:
        """普通 JSON 请求（非流式接口用）。"""
        headers = {}
        if self.auth is not None:
            headers = await self.auth.attach(headers)

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.request(method=method, url=f"{self.base_url}{path}", json=json_body, headers=headers)
            response.raise_for_status()
            return response.json() if response.content else {}

    async def stream_sse(self, method: str, path: str, *, json_body: dict[str, Any] | None = None):
        """标准 SSE 读取：httpx-sse 的 ``aconnect_sse``。

        依赖 ``httpx<0.28``（httpx 0.28 把 ``AsyncClient.stream`` 改成了异步
        迭代器，不再满足 httpx-sse 0.4.3 的 context-manager 协议——见
        spec §8.3）。事件名/内容一律从 ``data`` 里的 JSON 取（网关契约：
        事件名内嵌于 ``data.event``，而非 SSE 顶层 ``event:`` 行）。
        """
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            async with aconnect_sse(
                client,
                method,
                f"{self.base_url}{path}",
                json=json_body,
            ) as events:
                # 非 2xx：先让响应抛出来（httpx-sse 的 EventSource.response
                # 持有原始响应，不会自动 raise）。
                events.response.raise_for_status()
                async for sse in events.aiter_sse():
                    data_str = sse.data
                    if not data_str:
                        continue
                    try:
                        payload = json.loads(data_str)
                    except json.JSONDecodeError:
                        payload = {"raw": data_str}
                    # 事件名取 data.event（网关契约），SSE 顶层 event 行仅透传兼容
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
        """提交 turn：POST chat → SSE → 方言翻译为 RuntimeEvent 列表。

        流结束仍未收到 turn_completed 时补一条 turn_interrupted（断流兜底）。
        """
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
        """chat 的向后兼容包装（旧调用方使用）。"""
        return await self.chat(
            session_id=session_id,
            sandbox_id=str(payload.get("sandboxId", payload.get("sandbox_id", ""))),
            turn_id=turn_id,
            agent_md=str(payload.get("agentMd", payload.get("agent_md", ""))),
            history=list(payload.get("history", [])),
            user_message=str(payload.get("userMessage", payload.get("user_message", ""))),
        )
