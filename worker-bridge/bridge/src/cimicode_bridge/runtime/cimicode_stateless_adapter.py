"""cimicode-stateless adapter：外部无状态 cimicode 平台（Gateway v2 双接口）的对接形态。

绑定来源（runtime.yaml 顶层 bridge 段，controller 从 Worker CR
spec.runtimeParameter 整 map 投影——契约 v1.3；已知键 baseUrl/sessionId/
sandboxId/templateId/eid 填固定字段，追加键经 submit 信封透传平台）：
- base_url   外部 Gateway 接口地址
- session_id / sandbox_id   平台预建的会话/沙箱绑定
- template_id    agent 模板追溯
- eid    用户业务标识（Gateway v2 submit 必传 Header）

传输契约（Gateway v2，提交与订阅分离）：
1. ``POST /agi/gateway/v1/turn/submit``——异步受理，立即回执
   ``Result<TurnVO>{turnId, attemptId, queueStatus=QUEUED}``；
   Header 必带 ``eid`` + ``X-Idempotency-Key``（UUID v4，每 turn 新生成）；
   Body 为 ``Request<TurnSubmitDTO>`` 信封，严格三字段
   ``{data:{sessionId, agentPrompt, userMessage}}``——history/sandboxId/
   turnId 不传（Gateway 自管），runtimeParameter 追加键也不上车（参数面
   就是 runtime.yaml，bridge 抽固定字段自用，见 app._apply_bridge_section）。
2. ``GET /agi/gateway/v1/session/{sessionId}/events``——SSE 订阅事件流
   （全量重放 + 实时续读 + 终态关闭）；envelope 为 cimicode turn/1 原样
   JSON，``type`` 字段命名（``session.next.*@N`` / ``invocation.*``）。

终态语义：``invocation.idle/yielded/failed`` 互斥且恰一次，终态帧后服务端
主动关闭连接——这是正常结束；只有流断且未见终态帧才补 ``turn_interrupted``。
"""
from __future__ import annotations

import uuid
from typing import Any

from cimicode_bridge.events import RuntimeEvent, RuntimeEventKind
from cimicode_bridge.runtime.cimicode_adapter import CimicodeAdapter, GatewayV2Dialect

SUBMIT_PATH = "/agi/gateway/v1/turn/submit"


def events_path(session_id: str) -> str:
    """SSE 订阅路径（session 维度，非 turn 维度）。"""
    return f"/agi/gateway/v1/session/{session_id}/events"


class CimicodeStatelessAdapter(CimicodeAdapter):
    """无状态形态：submit 回执 + SSE 订阅两步化（Gateway v2 契约）。"""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: int = 600,
        auth: Any | None = None,
        eid: str = "",
    ) -> None:
        super().__init__(base_url, timeout_seconds=timeout_seconds, auth=auth)
        self.eid = eid  # 用户业务标识（调协者 env 注入；空 = 未配置，由 app 层门禁拒轮）

    async def chat(
        self,
        *,
        session_id: str,
        agent_md: str,
        user_message: str,
    ) -> list[RuntimeEvent]:
        """提交 turn 并消费事件流：submit 回执 → SSE 订阅 → 方言翻译。

        信封严格三字段（sessionId/agentPrompt/userMessage）；参数走
        runtime.yaml 袋（新约定），bridge 侧抽固定字段自用，袋内追加键
        不上车。

        流结束仍未见到终态帧时补一条 turn_interrupted（断流兜底）；
        终态帧后服务端关闭连接是正常结束，不补。
        """
        # ① 异步受理：submit 回执（幂等键每 turn 新生成，UUID v4）
        receipt = await self.request_json(
            "POST",
            SUBMIT_PATH,
            json_body={
                "data": {
                    "sessionId": session_id,
                    "agentPrompt": agent_md,
                    "userMessage": user_message,
                }
            },
            headers=self._headers(),
        )
        turn_id = str((receipt.get("data") or {}).get("turnId") or "")
        # ② SSE 订阅：session 维度事件流（重放 + 实时 + 终态关闭）
        events: list[RuntimeEvent] = []
        dialect = GatewayV2Dialect()
        async for line in self.stream_sse(
            "GET",
            events_path(session_id),
            headers=self._headers(),
        ):
            events.extend(dialect.translate(line))
        if not dialect.terminal_seen:
            events.append(
                RuntimeEvent(
                    kind=RuntimeEventKind.TURN_INTERRUPTED,
                    text=f"Gateway stream ended before terminal frame (turnId={turn_id})",
                )
            )
        return events

    def _headers(self) -> dict[str, str]:
        """公共 Header：eid（业务标识）+ X-Idempotency-Key（请求级幂等键）。"""
        headers: dict[str, str] = {"X-Idempotency-Key": str(uuid.uuid4())}
        if self.eid:
            headers["eid"] = self.eid
        return headers
