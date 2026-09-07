"""会话状态管理：CoPaw 三段式群聊视野 buffer + turn 记录。"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any


# CoPaw 约定的两段标记（与 OpenClaw 保持一致，便于 agent 统一解析）
HISTORY_CONTEXT_MARKER = "[Chat messages since your last reply - for context]"  # 历史上下文段
CURRENT_MESSAGE_MARKER = "[Current message - respond to this]"                  # 当前指令段


@dataclass
class HistoryStore:
    """单个 room 的群聊视野 buffer（内存、FIFO 滑窗、event_id 去重）。"""

    capacity: int = 200                                   # 最大条数（默认 50 由配置传入）
    _items: deque[dict[str, str]] = field(default_factory=deque)

    def append(self, role: str, content: str, *, event_id: str | None = None) -> None:
        """追加一条消息；同 event_id 的重放事件直接跳过；超限 FIFO 淘汰最旧。"""
        if event_id and any(item.get("event_id") == event_id for item in self._items):
            return
        item = {"role": role, "content": content}
        if event_id:
            item["event_id"] = event_id
        self._items.append(item)
        while len(self._items) > self.capacity:
            self._items.popleft()

    def all(self) -> list[dict[str, str]]:
        """返回 buffer 快照（浅拷贝列表）。"""
        return list(self._items)

    def build_context(self, current_message: str) -> str:
        """组装 CoPaw 三段式文本：[历史段] + [当前指令段]。"""
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
        """清空 buffer（turn 成功提交后调用）。"""
        self._items.clear()


@dataclass
class SessionManager:
    """进程内 turn 记录器（调试用途；真正的 Session 状态在 gateway 侧）。"""

    _sessions: dict[str, dict[str, Any]] = field(default_factory=dict)

    def start_session(self, session_id: str) -> None:
        """确保 session 记录存在（幂等）。"""
        self._sessions.setdefault(session_id, {"turns": [], "current_turn": None})

    def add_turn(self, session_id: str, turn_id: str, content: str) -> None:
        """记录一次 turn 并更新当前指针。"""
        session = self._sessions.setdefault(session_id, {"turns": [], "current_turn": None})
        session["turns"].append({"turn_id": turn_id, "content": content})
        session["current_turn"] = turn_id

    def current_turn(self, session_id: str) -> str | None:
        """返回该 session 最近一次 turn 的 ID。"""
        session = self._sessions.get(session_id)
        if not session:
            return None
        return session.get("current_turn")

    def turn_history(self, session_id: str) -> list[dict[str, str]]:
        """返回该 session 的全部 turn 记录。"""
        session = self._sessions.get(session_id)
        if not session:
            return []
        return list(session["turns"])
