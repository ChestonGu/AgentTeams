"""cimicode-stateless adapter：外部无状态 cimicode 平台（Gateway v2 双接口）的对接形态。

诊断日志约定（BRIDGE_SSE_TRACE=1 时逐帧打印原始事件，默认不打逐帧日志）：
submit 回执 / 流结束统计始终常驻 INFO——排查 "SSE 绑错 turn / 无终态帧 /
正文丢失" 类问题不再空口无凭。

绑定来源（runtime.yaml 顶层 bridge 段，controller 从 Worker CR
spec.runtimeParameter 整 map 投影——契约 v1.3；已知键 baseUrl/sessionId/
sandboxId/templateId/eid/appKey/appSecret/model 填固定字段，追加键经
submit 信封透传平台）：
- base_url   外部 Gateway 接口地址
- session_id / sandbox_id   平台预建的会话/沙箱绑定
- template_id    agent 模板追溯
- eid    用户业务标识（应用级鉴权经 X-Operator-Eid 头传）
- app_key / app_secret   应用级鉴权三件套（Gateway 运维分配）
- model   submit 信封 model 覆盖（空=用 Session 冻结值）

传输契约（Gateway v2，提交与订阅分离）：
1. ``POST /agi/gateway/v1/turn/submit``——异步受理，立即回执
   ``Result<TurnVO>{code, message, data:{turnId, attemptId,
   queueStatus=QUEUED}}``；
   Header 必带应用级鉴权三件套（``X-App-Key``/``X-App-Secret``/
   ``X-Operator-Eid``）+ ``X-Idempotency-Key``（UUID v4，每 turn 新生成）；
   Body 为 ``TurnSubmitDTO`` **顶层平铺** ``{sessionId, agentPrompt,
   userMessage, model?}``——联调说明书写的 ``Request<TurnSubmitDTO>``
   ``{data:{...}}`` 信封与 Gateway 实际实现不符（实测 2026-09-28：
   信封形态返回 HTTP 200 + ``code:"sessionId不能为空"``，业务字段
   全部丢失）；history/sandboxId/turnId 不传（Gateway 自管），
   runtimeParameter 追加键也不上车（参数面就是 runtime.yaml，
   bridge 抽固定字段自用，见 app._apply_bridge_section）。
   注意 Result 信封的 ``code`` 是**业务状态**：HTTP 200 不代表受理
   成功，``code != "SUCCESS"`` 时 ``data`` 为空——chat() 显式校验。
2. ``GET /agi/gateway/v1/sse/session/{sessionId}/events``——SSE 订阅
   事件流（全量重放 + 实时续读 + 终态关闭）；envelope 为 cimicode
   turn/1 原样 JSON，``type`` 字段命名（``session.next.*@N`` /
   ``invocation.*``）。注意路径带 ``/sse/`` 段（Spring MVC 端口，
   Ingress 按路径分流，与 Dubbo REST 前缀共用域名）。

终态语义：``invocation.idle/yielded/failed`` 互斥且恰一次，终态帧后服务端
主动关闭连接——这是正常结束；只有流断且未见终态帧才补 ``turn_interrupted``。
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Any

logger = logging.getLogger(__name__)

from cimicode_bridge.events import RuntimeEvent, RuntimeEventKind
from cimicode_bridge.runtime.cimicode_adapter import CimicodeAdapter, GatewayV2Dialect

SUBMIT_PATH = "/agi/gateway/v1/turn/submit"


def events_path(session_id: str) -> str:
    """SSE 订阅路径（session 维度，非 turn 维度）。

    Gateway 现行路径带 ``/sse/`` 段（SessionEventController，Spring MVC
    端口 8080，Ingress 按路径分流）——与 Dubbo REST 前缀 ``/agi/gateway/v1/``
    共用域名但路径不同：``/agi/gateway/v1/sse/session/{id}/events``。
    """
    return f"/agi/gateway/v1/sse/session/{session_id}/events"


class CimicodeStatelessAdapter(CimicodeAdapter):
    """无状态形态：submit 回执 + SSE 订阅两步化（Gateway v2 契约）。"""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: int = 600,
        auth: Any | None = None,
        eid: str = "",
        app_key: str = "",
        app_secret: str = "",
        api_key: str = "",
        model: str = "",
    ) -> None:
        super().__init__(base_url, timeout_seconds=timeout_seconds, auth=auth)
        self.eid = eid  # 用户业务标识（调协者 env 注入；空 = 未配置，由 app 层门禁拒轮）
        # 应用级鉴权三件套（旧联调说明书改动②，鉴权上移 APISIX 后仅兼容旧网关）：
        # X-App-Key / X-App-Secret / X-Operator-Eid——eid 经 Operator 头传，
        # 不再裸传 ``eid`` header（Gateway 已不认裸 eid，会 401）。
        self.app_key = app_key
        self.app_secret = app_secret
        # APISIX OpenAPI key-auth（2026-10-08 鉴权上移）：apiKey 经 ``x-api-key``
        # 头传，认证服务身份；eid 仍需随请求传（Gateway 优先采信 eid 头，
        # X-Operator-Eid 回退），Session 归属校验用。
        self.api_key = api_key
        # model 覆盖（联调说明书改动③）：不传则用 Session 冻结的 model；
        # 当前测试环境模板冻结的 glm-4.7 不在 Runtime 目录，需显式传
        # deepseek-v4-flash 绕开（AgentHub 模板修正后可清空）。
        self.model = model

    async def chat(
        self,
        *,
        session_id: str,
        agent_md: str,
        user_message: str,
        on_event: Any | None = None,
    ) -> list[RuntimeEvent]:
        """提交 turn 并消费事件流：submit 回执 → SSE 订阅 → 方言翻译。

        信封严格三字段顶层平铺（sessionId/agentPrompt/userMessage[+
        model]）；参数走 runtime.yaml 袋（新约定），bridge 侧抽固定字段
        自用，袋内追加键不上车。

        ``on_event``：可选异步回调 ``async on_event(event: RuntimeEvent)``——
        SSE 消费循环中每翻译出一个事件即调用（TEXT_DONE 到达时实时透出，
        上层可据此流式编辑 Matrix 消息）。回调异常不中断消费（降级为
        攒齐返回的批处理模式）。

        Result 信封业务校验：HTTP 200 不代表受理成功——``code !=
        "SUCCESS"``（如 ``"sessionId不能为空"``）时 ``data`` 为空，
        必须显式抛错，否则错误延迟到 SSE 订阅 404 才暴露（难排查）。

        流结束仍未见到终态帧时补一条 turn_interrupted（断流兜底）；
        终态帧后服务端关闭连接是正常结束，不补。
        """
        # ① 异步受理：submit 回执（幂等键每 turn 新生成，UUID v4）
        submit_body: dict[str, Any] = {
            "sessionId": session_id,
            "agentPrompt": agent_md,
            "userMessage": user_message,
        }
        if self.model:
            # Gateway：不传 model 则用 Session 冻结的 model；显式传可覆盖
            # （测试环境模板冻结的 glm-4.7 不在 Runtime 目录，联调期必传）
            submit_body["model"] = self.model
        receipt = await self.request_json(
            "POST",
            SUBMIT_PATH,
            json_body=submit_body,
            headers=self._headers(),
        )
        # Result 信封业务校验（Gateway 对非法 body 也回 HTTP 200 +
        # code!=SUCCESS，如信封形态实测返回 code="sessionId不能为空"）
        biz_code = str(receipt.get("code") or "")
        if biz_code and biz_code != "SUCCESS":
            raise RuntimeError(
                f"Gateway submit rejected: code={biz_code!r} "
                f"message={receipt.get('message')!r} (session={session_id})"
            )
        receipt_data = receipt.get("data") or {}
        turn_id = str(receipt_data.get("turnId") or "")
        logger.info(
            "stateless submit receipt: session=%s turnId=%s attemptId=%s queueStatus=%s model=%s",
            session_id, turn_id, receipt_data.get("attemptId"),
            receipt_data.get("queueStatus"), self.model or "(session-frozen)",
        )
        # ② SSE 订阅：session 维度事件流（重放 + 实时 + 终态关闭）
        events: list[RuntimeEvent] = []
        dialect = GatewayV2Dialect()
        frame_count = 0
        last_frame_type = "(none)"
        # 帧日志开关：BRIDGE_SSE_TRACE=1 逐帧打印原始事件（排障时开），
        # 默认不打逐帧日志——只留 submit 回执 + 流结束统计。
        trace = os.environ.get("BRIDGE_SSE_TRACE", "").strip() == "1"
        async for line in self.stream_sse(
            "GET",
            events_path(session_id),
            headers=self._headers(),
        ):
            frame_count += 1
            envelope = line.get("data", line) if isinstance(line.get("data"), dict) else {}
            evt_type = str(envelope.get("type") or "")
            evt_inv = str(envelope.get("inv") or "")
            evt_seq = envelope.get("seq")
            last_frame_type = evt_type or "(unknown)"
            if trace:
                # 逐帧原始事件（含 delta/全文内容）——排障时开，平时关
                logger.info(
                    "stateless sse frame #%d: session=%s type=%s inv=%s seq=%s raw=%s",
                    frame_count, session_id, evt_type, evt_inv, evt_seq,
                    str(line.get("data"))[:500],
                )
            for translated in dialect.translate(line):
                events.append(translated)
                # 实时透出：TEXT_DONE（一段完整叙述收口）到达即回调——
                # 上层可据此流式编辑 Matrix 消息（对齐 CoPaw 的体验）。
                # 回调异常不中断消费（批处理模式兜底）。
                if on_event is not None and translated.kind == RuntimeEventKind.TEXT_DONE:
                    try:
                        await on_event(translated)
                    except Exception:
                        logger.exception("on_event callback failed; falling back to batch mode")
        # 流结束统计：帧数 / 终态 / 正文聚合结果——一眼看出绑错 turn / 无终态 / 正文丢失
        body_text = "\n".join(dialect.parts[pid] for pid in dialect.part_order)
        logger.info(
            "stateless sse stream ended: session=%s submitTurnId=%s frames=%d "
            "terminalSeen=%s bodyParts=%d bodyChars=%d reasoningParts=%d "
            "lastFrameType=%s",
            session_id, turn_id, frame_count, dialect.terminal_seen,
            len(dialect.part_order), len(body_text), len(dialect._reasoning_parts),
            last_frame_type,
        )
        if not dialect.terminal_seen:
            events.append(
                RuntimeEvent(
                    kind=RuntimeEventKind.TURN_INTERRUPTED,
                    text=f"Gateway stream ended before terminal frame (turnId={turn_id})",
                )
            )
        return events

    def _headers(self) -> dict[str, str]:
        """公共 Header：鉴权凭证 + X-Idempotency-Key（请求级幂等键）。

        鉴权形态三选一（互斥，优先级从高到低）：
        ① ``api_key`` 配置——APISIX OpenAPI key-auth（2026-10-08 鉴权上移
           后的现行形态）：``x-api-key`` 认证服务身份 + 裸 ``eid`` 头代表
           最终用户（Gateway 优先采信 eid，X-Operator-Eid 仅回退）；
        ② ``app_key``/``app_secret`` 配置——旧网关应用级三件套
           （X-App-Key/X-App-Secret/X-Operator-Eid），兼容旧部署；
        ③ 仅 ``eid``——裸 eid header（本地 mock / 无鉴权网关）。
        """
        headers: dict[str, str] = {"X-Idempotency-Key": str(uuid.uuid4())}
        if self.api_key:
            headers["x-api-key"] = self.api_key
            if self.eid:
                headers["eid"] = self.eid
        elif self.app_key and self.app_secret:
            headers["X-App-Key"] = self.app_key
            headers["X-App-Secret"] = self.app_secret
            if self.eid:
                headers["X-Operator-Eid"] = self.eid
        elif self.eid:
            headers["eid"] = self.eid
        return headers
