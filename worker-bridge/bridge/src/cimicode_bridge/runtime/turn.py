"""Gateway 单轮对话执行：agentMd 组装 → chat SSE → 事件聚合为回复文本。

本模块只负责"一轮对话怎么调 gateway、怎么读结果"；
过滤决策在 matrix/filter，群聊视野 buffer 在 session，应用编排在 app。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from cimicode_bridge.bootstrap import WorkerBootstrapConfig
from cimicode_bridge.config import RuntimeConfig
from cimicode_bridge.events import RuntimeEvent
from cimicode_bridge.render import build_agent_md

logger = logging.getLogger(__name__)


@dataclass
class TurnResult:
    """单轮 gateway 调用结果。

    text   = 聚合后的回复文本（turn_completed 优先，否则 delta 顺序拼接）
    failed = turn 是否失败（收到 runtime_error / turn_interrupted）
    error  = 失败详情（data 或 text，仅 failed=True 时有意义）
    """

    text: str = ""
    failed: bool = False
    error: str = ""


class TurnRunner:
    """单轮对话执行器（无状态：client 与 worker_files 均由调用方每轮传入，方便测试替换 fake）。

    config 为启动时的 RuntimeConfig 引用（session_id/sandbox_id 由 S3 覆盖后同步生效）。
    """

    def __init__(self, *, config: RuntimeConfig) -> None:
        self.config = config

    @staticmethod
    def build_agent_md(worker_files: WorkerBootstrapConfig | None, room_id: str) -> str:
        """组装本轮 agentMd：协作上下文（COORDINATION_* env）+ AGENTS.md + SOUL.md。

        每轮全量重拼（内容源为启动时缓存的 S3 文件），gateway 侧作为 system 指令透传。
        """
        return build_agent_md(
            agents_md=worker_files.agents_md if worker_files else "",
            soul_md=worker_files.soul_md if worker_files else "",
            role=os.getenv("COORDINATION_ROLE", "worker"),
            leader=os.getenv("COORDINATION_LEADER", ""),
            team=os.getenv("COORDINATION_TEAM", ""),
            room=os.getenv("COORDINATION_ROOM", room_id),
            admin=os.getenv("COORDINATION_ADMIN", ""),
            workers=os.getenv("COORDINATION_WORKERS", ""),
        )

    async def run_turn(
        self,
        client,
        *,
        worker_files: WorkerBootstrapConfig | None,
        room_id: str,
        event_id: str,
        user_message: str,
    ) -> TurnResult:
        """调 gateway chat 并把 SSE 事件聚合为回复文本。

        聚合规则（与重构前 app.py 完全一致）：
        - text_delta      → 追加
        - turn_completed  → 覆盖（gateway 的 done.content 是权威全文）
        - runtime_error / turn_interrupted → failed=True，保留已聚合文本便于排查
        """
        events = await client.chat(
            session_id=self.config.session_id,
            sandbox_id=self.config.sandbox_id,
            turn_id=event_id,  # turnId = Matrix event_id（幂等键）
            agent_md=self.build_agent_md(worker_files, room_id),
            history=[],
            user_message=user_message,
        )
        return self.aggregate_reply(events)

    @staticmethod
    def aggregate_reply(events: list[RuntimeEvent]) -> TurnResult:
        """把 RuntimeEvent 列表聚合为 TurnResult（纯函数，便于单测）。"""
        response_text = ""
        for event in events:
            if event.kind.value == "text_delta":
                response_text += event.text
            elif event.kind.value == "turn_completed":
                response_text = event.text or response_text
            elif event.kind.value in {"runtime_error", "turn_interrupted"}:
                logger.error("Gateway turn failed: %s", event.data or event.text)
                return TurnResult(text=response_text, failed=True, error=str(event.data or event.text))
        return TurnResult(text=response_text)
