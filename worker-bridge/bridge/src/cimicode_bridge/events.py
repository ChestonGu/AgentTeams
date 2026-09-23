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


class MatrixMessage(BaseModel):
    """Matrix 入站消息的规范化表示（HTTP 测试通道 / 回放用途）。"""

    event_id: str              # Matrix 事件 ID
    room_id: str               # 房间 ID
    sender: str                # 发送者 MXID
    sender_display_name: str | None = None  # 显示名（可选）
    body: str                  # 纯文本正文
    timestamp: int | None = None             # 服务器时间戳（毫秒）
    mentions: list[str] = Field(default_factory=list)  # 正文/结构化 mention 列表


class MatrixMessage(BaseModel):
    """入站 Matrix 文本消息的规范化表示。"""

    event_id: str
    room_id: str
    sender: str
    sender_display_name: str | None = None
    body: str
    timestamp: int | None = None
    mentions: list[str] = Field(default_factory=list)
