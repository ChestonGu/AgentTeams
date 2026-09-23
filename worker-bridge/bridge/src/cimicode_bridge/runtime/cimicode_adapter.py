"""cimicode adapter：HTTP/SSE 传输层 + SSE 事件方言翻译（一个 runtime 一个文件）。

CimicodeAdapter（传输层）：gateway 通用客户端，JSON 请求 + SSE 流消费。
GatewayV2Dialect（翻译层）：Gateway v2 turn/1 envelope → RuntimeEvent（含 part 缓冲聚合）。
"""
from __future__ import annotations

import json
from typing import Any

import httpx
from httpx_sse import aconnect_sse

from cimicode_bridge.events import RuntimeEvent, RuntimeEventKind


class CimicodeAdapter:
    """cimicode gateway 客户端（HTTP/SSE 传输）+ 方言翻译。

    对应 spec §3.1 Runtime SPI 的 cimicode 实现：request_json/stream_sse
    传输基座 + GatewayV2Dialect 翻译；两步化编排（submit 回执 + SSE 订阅）
    在 CimicodeStatelessAdapter 子类。
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

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """普通 JSON 请求（非流式接口用；headers 供 eid / 幂等键等透传）。"""
        merged = dict(headers or {})
        if self.auth is not None:
            merged = await self.auth.attach(merged)

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.request(method=method, url=f"{self.base_url}{path}", json=json_body, headers=merged)
            response.raise_for_status()
            return response.json() if response.content else {}

    async def stream_sse(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ):
        """标准 SSE 读取：httpx-sse 的 ``aconnect_sse``。

        依赖 ``httpx<0.28``（httpx 0.28 把 ``AsyncClient.stream`` 改成了异步
        迭代器，不再满足 httpx-sse 0.4.3 的 context-manager 协议——见
        spec §8.3）。事件名/内容一律从 ``data`` 里的 JSON 取（网关契约：
        事件名内嵌于 envelope 的 ``type`` 字段，而非 SSE 顶层 ``event:`` 行）。
        """
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            async with aconnect_sse(
                client,
                method,
                f"{self.base_url}{path}",
                json=json_body,
                headers=headers,
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



class GatewayV2Dialect:
    """Gateway v2 方言翻译器：turn/1 envelope（``type`` 字段）→ RuntimeEvent。

    envelope 契约（Gateway SseEventForwarder 原样转发 cimicode turn/1 帧）：
    ``{kind, sid, inv, epoch, seq, type, data, rev?, eventID?}``。
    事件名取 envelope 的 ``type``（如 ``session.next.text.delta@`` 带版本
    命名）；终态 = ``invocation.idle / yielded / failed``（互斥且恰一次，
    终态帧后服务端主动关闭连接——这是正常结束，不是断流）。
    """

    name = "gateway-v2"

    # 终态 type 集合（turn/1 契约 §7）
    TERMINAL_TYPES = {"invocation.idle", "invocation.yielded", "invocation.failed"}

    def __init__(self) -> None:
        self.parts: dict[str, str] = {}      # part_id → 已累积文本
        self.part_order: list[str] = []      # part 首次出现顺序（终态按此拼接）
        self._closed_parts: set[str] = set() # 已收口（ended）的 part，后续 delta 不再追加
        self.terminal_seen = False          # 是否已见终态帧

    def is_terminal(self, event_type: str) -> bool:
        """终态判定（含 v0.1 遗留 done/error 兼容）。"""
        return event_type in self.TERMINAL_TYPES or event_type in {"done", "error"}

    def translate(self, raw_event: dict[str, Any]) -> list[RuntimeEvent]:
        """翻译单个 envelope 为 RuntimeEvent（列表包装保持接口统一）。"""
        envelope = raw_event.get("data", raw_event)
        if not isinstance(envelope, dict):
            envelope = {"value": envelope}
        event_type = str(envelope.get("type") or "")
        payload = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}

        if event_type in {
            "session.next.text.delta",
            "session.next.reasoning.delta",
            "message.part.delta",
            "message",
        }:
            text = str(payload.get("delta") or payload.get("text") or "")
            kind = RuntimeEventKind.TEXT_DELTA
        elif event_type in {"session.next.text.ended@1", "message.part.updated"}:
            # durable 全文收口帧：part 全量替换
            part = payload.get("part") if isinstance(payload.get("part"), dict) else {}
            text = str(part.get("text") or payload.get("text") or "")
            kind = RuntimeEventKind.TEXT_DONE
        elif event_type in {"invocation.idle", "invocation.yielded", "done"}:
            # 正常终态：done.content / idle 无正文时按 part 首现顺序拼接
            text = str(payload.get("content") or payload.get("text") or "")
            kind = RuntimeEventKind.TURN_COMPLETED
            self.terminal_seen = True
        elif event_type in {"invocation.failed", "error", "session.error"}:
            error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            text = str(error.get("message") or payload.get("message") or payload.get("reason") or "")
            kind = RuntimeEventKind.RUNTIME_ERROR
            self.terminal_seen = True
        elif event_type == "session.next.tool.called@1":
            text = str(payload.get("tool") or "")
            kind = RuntimeEventKind.TOOL_STARTED
        elif event_type in {"session.next.tool.success@1", "session.next.tool.failed@1"}:
            text = str(payload.get("output") or payload.get("error") or "")
            kind = RuntimeEventKind.TOOL_FINISHED
        elif event_type == "invocation.started":
            kind = RuntimeEventKind.TURN_STARTED
            text = ""
        else:
            # 未识别事件（turn.accepted / status.changed / diagnostic / live 增量等）：
            # 不产生 RuntimeEvent，原文已在 envelope 里可诊断，不丢失也不误报错
            return []

        # part 缓冲聚合：带 part_id 的事件按 delta 追加 / ended 全量替换；
        # ended 后该 part 已收口（durable 全文），后续 delta 不再追加。
        # part_id 位置两种：delta 帧在 payload 顶层；ended 帧在 part 对象内部。
        part_obj = payload.get("part") if isinstance(payload.get("part"), dict) else {}
        part_id = str(
            payload.get("partID")
            or payload.get("part_id")
            or part_obj.get("partID")
            or part_obj.get("part_id")
            or part_obj.get("id")
            or ""
        )
        if part_id and kind in {RuntimeEventKind.TEXT_DELTA, RuntimeEventKind.TEXT_DONE}:
            if part_id not in self.parts:
                self.part_order.append(part_id)
            if kind == RuntimeEventKind.TEXT_DONE:
                self.parts[part_id] = str(text)
                self._closed_parts.add(part_id)
            elif part_id not in self._closed_parts:
                self.parts[part_id] = self.parts.get(part_id, "") + str(text)
        # 终态未带全文时：按 part 首现顺序拼接聚合文本
        if kind == RuntimeEventKind.TURN_COMPLETED and not text:
            text = "\n".join(self.parts[part_id] for part_id in self.part_order)
        return [
            RuntimeEvent(
                seq=envelope.get("seq"),
                kind=kind,
                text=str(text),
                data=envelope,
            )
        ]
