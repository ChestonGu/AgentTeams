"""事件方言：cimicode SSE 事件 → RuntimeEvent 的翻译（含 part 缓冲聚合）。"""
from __future__ import annotations

from typing import Any

from cimicode_bridge.events import RuntimeEvent, RuntimeEventKind
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