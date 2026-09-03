from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class RuntimeEventKind(str, Enum):
    TURN_STARTED = "turn_started"
    TEXT_DELTA = "text_delta"
    TEXT_DONE = "text_done"
    TOOL_STARTED = "tool_started"
    TOOL_FINISHED = "tool_finished"
    ARTIFACT_PUBLISHED = "artifact_published"
    TURN_COMPLETED = "turn_completed"
    TURN_INTERRUPTED = "turn_interrupted"
    RUNTIME_ERROR = "runtime_error"


class RuntimeEvent(BaseModel):
    seq: int | None = None
    kind: RuntimeEventKind
    text: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class HistoryMessage(BaseModel):
    role: str
    content: str
    event_id: str | None = None


class ChatRequest(BaseModel):
    session_id: str
    sandbox_id: str | None = None
    turn_id: str
    agent_md: str = ""
    history: list[HistoryMessage] = Field(default_factory=list)
    user_message: str = ""


class MatrixMessage(BaseModel):
    event_id: str
    room_id: str
    sender: str
    sender_display_name: str | None = None
    body: str
    timestamp: int | None = None
    mentions: list[str] = Field(default_factory=list)
