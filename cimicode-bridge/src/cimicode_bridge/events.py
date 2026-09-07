"""统一事件与消息模型（渲染/发送层只认这里的"普通话"）。"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class RuntimeEventKind(str, Enum):
    """RuntimeEvent 的种类（各 runtime 方言统一翻译到这里）。"""

    TURN_STARTED = "turn_started"            # turn 开始
    TEXT_DELTA = "text_delta"                # 文本增量
    TEXT_DONE = "text_done"                  # 一段文本完成（part 全量替换）
    TOOL_STARTED = "tool_started"            # 工具调用开始
    TOOL_FINISHED = "tool_finished"          # 工具调用结束
    ARTIFACT_PUBLISHED = "artifact_published"  # 产物发布
    TURN_COMPLETED = "turn_completed"        # turn 正常完成（含完整文本）
    TURN_INTERRUPTED = "turn_interrupted"    # turn 中断（断流/超时）
    RUNTIME_ERROR = "runtime_error"          # 运行时错误


class RuntimeEvent(BaseModel):
    """运行时事件的统一表示；data 保留方言原始事件（诊断/透传）。"""

    seq: int | None = None     # event_seq（服务有则透传）
    kind: RuntimeEventKind
    text: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class HistoryMessage(BaseModel):
    """history 数组的单条消息（gateway 透传格式）。"""

    role: str                  # user / assistant
    content: str
    event_id: str | None = None


class ChatRequest(BaseModel):
    """gateway chat 请求四元组模型（契约 v0.2）。"""

    session_id: str
    sandbox_id: str | None = None
    turn_id: str               # 幂等键（= Matrix event_id）
    agent_md: str = ""         # 系统指令层（每轮全量重拼）
    history: list[HistoryMessage] = Field(default_factory=list)  # 历史层（当前恒空）
    user_message: str = ""     # 当前消息层（三段式群聊视野）


class MatrixMessage(BaseModel):
    """入站 Matrix 文本消息的规范化表示。"""

    event_id: str
    room_id: str
    sender: str
    sender_display_name: str | None = None
    body: str
    timestamp: int | None = None
    mentions: list[str] = Field(default_factory=list)
