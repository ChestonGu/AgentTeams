"""cimicode adapter：HTTP/SSE 传输层 + SSE 事件方言翻译（一个 runtime 一个文件）。

CimicodeAdapter（传输层）：gateway 通用客户端，JSON 请求 + SSE 流消费。
CimicodeDialect（翻译层）：cimicode SSE 事件 → RuntimeEvent（含 part 缓冲聚合）。
"""
from __future__ import annotations

import json
from typing import Any

import httpx
from httpx_sse import aconnect_sse

from cimicode_bridge.events import RuntimeEvent, RuntimeEventKind


class CimicodeAdapter:
    """cimicode gateway 客户端（HTTP/SSE 传输）+ 方言翻译。

    对应 spec §3.1 Runtime SPI 的 cimicode 实现：chat() 提交 turn →
    SSE 流式读取 → CimicodeDialect 翻译为 RuntimeEvent 列表。
    """

    name = "cimicode"

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


class CimicodeDialect:
    """cimicode 方言翻译器：message/done/error + part_id 缓冲聚合。

    事件名兼容两种位置：SSE 顶层 event 字段（httpx-sse 风格）
    或内嵌于 data.event（手写解析 / gateway 契约格式）。
    """

    name = "cimicode"

    def __init__(self) -> None:
        self.parts: dict[str, str] = {}      # part_id → 已累积文本
        self.part_order: list[str] = []      # part 首次出现顺序（done 按此拼接）

    def translate(self, raw_event: dict[str, Any]) -> list[RuntimeEvent]:
        """翻译单个 SSE 事件为 RuntimeEvent（列表包装保持接口统一）。"""
        data = raw_event.get("data", raw_event)
        if not isinstance(data, dict):
            data = {"value": data}
        # event 名可能在顶层（httpx-sse 风格）或内嵌于 data.event（手写解析/gateway 契约）
        event_name = str(
            raw_event.get("event")
            or data.get("event")
            or data.get("kind")
            or ""
        )
        # 事件映射：message* → 增量；done → 完成；error → 错误
        if event_name in {"message", "message.part.delta", "message.updated"}:
            text = data.get("delta", data.get("content", data.get("text", "")))
            kind = RuntimeEventKind.TEXT_DELTA
        elif event_name == "message.part.updated":
            text = data.get("text", data.get("content", ""))
            kind = RuntimeEventKind.TEXT_DONE
        elif event_name == "done":
            text = data.get("content", "")
            kind = RuntimeEventKind.TURN_COMPLETED
        elif event_name in {"error", "session.error"}:
            text = data.get("message", "")
            kind = RuntimeEventKind.RUNTIME_ERROR
        else:
            # 未识别事件：尝试按内部 kind 解析，否则包成 runtime_error（原文进 data 不丢弃）
            kind = RuntimeEventKind(raw_event.get("kind", "text_done")) if raw_event.get("kind") in RuntimeEventKind._value2member_map_ else RuntimeEventKind.RUNTIME_ERROR
            text = raw_event.get("text", "")
        # part 缓冲聚合：带 part_id 的事件按 delta 追加 / updated 全量替换
        part_id = str(data.get("part_id", ""))
        if part_id:
            if part_id not in self.parts:
                self.part_order.append(part_id)
            if event_name == "message.part.updated":
                self.parts[part_id] = str(text)
            else:
                self.parts[part_id] = self.parts.get(part_id, "") + str(text)
        # done 未带全文时：按 part 首现顺序拼接聚合文本
        if event_name == "done" and not text:
            text = "\n".join(self.parts[part_id] for part_id in self.part_order)
        return [
            RuntimeEvent(
                seq=raw_event.get("seq", data.get("event_seq")),
                kind=kind,
                text=str(text),
                data=data,
            )
        ]