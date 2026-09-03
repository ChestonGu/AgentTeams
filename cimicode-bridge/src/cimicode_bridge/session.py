from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any


HISTORY_CONTEXT_MARKER = "[Chat messages since your last reply - for context]"
CURRENT_MESSAGE_MARKER = "[Current message - respond to this]"


@dataclass
class HistoryStore:
    capacity: int = 200
    _items: deque[dict[str, str]] = field(default_factory=deque)

    def append(self, role: str, content: str, *, event_id: str | None = None) -> None:
        if event_id and any(item.get("event_id") == event_id for item in self._items):
            return
        item = {"role": role, "content": content}
        if event_id:
            item["event_id"] = event_id
        self._items.append(item)
        while len(self._items) > self.capacity:
            self._items.popleft()

    def all(self) -> list[dict[str, str]]:
        return list(self._items)

    def build_context(self, current_message: str) -> str:
        history = "\n".join(
            f"{item['role']}: {item['content']}" for item in self._items
        )
        if not history:
            return f"{CURRENT_MESSAGE_MARKER}\n{current_message}"
        return (
            f"{HISTORY_CONTEXT_MARKER}\n{history}\n\n"
            f"{CURRENT_MESSAGE_MARKER}\n{current_message}"
        )

    def clear(self) -> None:
        self._items.clear()


@dataclass
class SessionManager:
    _sessions: dict[str, dict[str, Any]] = field(default_factory=dict)

    def start_session(self, session_id: str) -> None:
        self._sessions.setdefault(session_id, {"turns": [], "current_turn": None})

    def add_turn(self, session_id: str, turn_id: str, content: str) -> None:
        session = self._sessions.setdefault(session_id, {"turns": [], "current_turn": None})
        session["turns"].append({"turn_id": turn_id, "content": content})
        session["current_turn"] = turn_id

    def current_turn(self, session_id: str) -> str | None:
        session = self._sessions.get(session_id)
        if not session:
            return None
        return session.get("current_turn")

    def turn_history(self, session_id: str) -> list[dict[str, str]]:
        session = self._sessions.get(session_id)
        if not session:
            return []
        return list(session["turns"])
