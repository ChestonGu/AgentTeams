from __future__ import annotations

from typing import Any

from cimicode_bridge.events import RuntimeEvent, RuntimeEventKind
from cimicode_bridge.runtime.base import EventDialect


class CimicodeDialect:
    name = "cimicode"

    def __init__(self) -> None:
        self.parts: dict[str, str] = {}
        self.part_order: list[str] = []

    def translate(self, raw_event: dict[str, Any]) -> list[RuntimeEvent]:
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
            kind = RuntimeEventKind(raw_event.get("kind", "text_done")) if raw_event.get("kind") in RuntimeEventKind._value2member_map_ else RuntimeEventKind.RUNTIME_ERROR
            text = raw_event.get("text", "")
        part_id = str(data.get("part_id", ""))
        if part_id:
            if part_id not in self.parts:
                self.part_order.append(part_id)
            if event_name == "message.part.updated":
                self.parts[part_id] = str(text)
            else:
                self.parts[part_id] = self.parts.get(part_id, "") + str(text)
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


class GenericSseDialect(EventDialect):
    name = "generic-sse"

    def translate(self, raw_event: dict[str, Any]) -> list[RuntimeEvent]:
        return [RuntimeEvent(kind=RuntimeEventKind.TEXT_DONE, text=str(raw_event.get("text", "")), data=raw_event)]
