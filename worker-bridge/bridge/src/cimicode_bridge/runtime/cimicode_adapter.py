"""cimicode adapter：HTTP/SSE 传输层 + SSE 事件方言翻译（一个 runtime 一个文件）。

CimicodeAdapter（传输层）：gateway 通用客户端，JSON 请求 + SSE 流消费。
GatewayV2Dialect（翻译层）：Gateway v2 turn/1 envelope → RuntimeEvent（含 part 缓冲聚合）。
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from httpx_sse import aconnect_sse

from cimicode_bridge.events import RuntimeEvent, RuntimeEventKind

logger = logging.getLogger(__name__)

# 出站请求日志的敏感头脱敏表（小写）：值替换为前4位+长度摘要——
# 排障要能对上"传的是哪个 key"，但不能把完整凭证泄进日志。
_REDACT_HEADERS = {"x-api-key", "x-app-key", "x-app-secret", "authorization"}


def _redact_headers(headers: dict[str, str] | None) -> dict[str, str]:
    """敏感头脱敏：保留前 4 字符 + 总长，够对账不够泄密。"""
    out: dict[str, str] = {}
    for name, value in (headers or {}).items():
        if name.lower() in _REDACT_HEADERS and value:
            out[name] = f"{value[:4]}...(len={len(value)})"
        else:
            out[name] = value
    return out


def _summarize_body(json_body: dict[str, Any] | None, limit: int = 200) -> str:
    """请求体摘要：每字段截断 + 总长标记，超限整体截断。

    submit 信封的 agentPrompt（完整 agent.md）动辄上万字符——逐字段
    截断后仍能看出"传了哪些字段、各多大"，但不会刷屏。字段值超
    ``limit`` 的显示前 limit 字符 + 总长。
    """
    if not json_body:
        return str(json_body)
    parts = []
    for key, value in json_body.items():
        text = str(value)
        if len(text) > limit:
            parts.append(f"{key}={text[:limit]}...(len={len(text)})")
        else:
            parts.append(f"{key}={text}")
    summary = " ".join(parts)
    if len(summary) > 2000:
        summary = summary[:2000] + "...(truncated)"
    return summary


class CimicodeAdapter:
    """cimicode gateway 客户端（HTTP/SSE 传输）+ 方言翻译。

    对应 spec §3.1 Runtime SPI 的 cimicode 实现：request_json/stream_sse
    传输基座 + GatewayV2Dialect 翻译；两步化编排（submit 回执 + SSE 订阅）
    在 CimicodeStatelessAdapter 子类。
    """

    name = "cimicode"

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: int = 600,
        auth: Any | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds  # 流读超时 = turn 超时
        self.auth = auth                        # AuthProvider（当前 None）

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """普通 JSON 请求（非流式接口用；headers 供 eid / 幂等键等透传）。"""
        merged = dict(headers or {})
        if self.auth is not None:
            merged = await self.auth.attach(merged)
        # 出站请求一行日志：url + headers（敏感头脱敏）+ body 摘要——
        # 联调排障第一现场（401/403/信封错形态一眼定位）。
        logger.info(
            "gateway request: %s %s%s headers=%s body=%s",
            method, self.base_url, path, _redact_headers(merged), _summarize_body(json_body),
        )

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.request(method=method, url=f"{self.base_url}{path}", json=json_body, headers=merged)
            response.raise_for_status()
            return response.json() if response.content else {}

    async def stream_sse(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ):
        """标准 SSE 读取：httpx-sse 的 ``aconnect_sse``。

        依赖 ``httpx<0.28``（httpx 0.28 把 ``AsyncClient.stream`` 改成了异步
        迭代器，不再满足 httpx-sse 0.4.3 的 context-manager 协议——见
        spec §8.3）。事件名/内容一律从 ``data`` 里的 JSON 取（网关契约：
        事件名内嵌于 envelope 的 ``type`` 字段，而非 SSE 顶层 ``event:`` 行）。
        """
        # 出站订阅一行日志：url + headers（敏感头脱敏）——SSE 无 body
        logger.info(
            "gateway request: %s %s%s headers=%s body=%s",
            method, self.base_url, path, _redact_headers(headers), _summarize_body(json_body),
        )
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            async with aconnect_sse(
                client,
                method,
                f"{self.base_url}{path}",
                json=json_body,
                headers=headers,
            ) as events:
                # 非 2xx：先让响应抛出来（httpx-sse 的 EventSource.response
                # 持有原始响应，不会自动 raise）。
                events.response.raise_for_status()
                async for sse in events.aiter_sse():
                    data_str = sse.data
                    if not data_str:
                        continue
                    try:
                        payload = json.loads(data_str)
                    except json.JSONDecodeError:
                        payload = {"raw": data_str}
                    # 事件名取 data.event（网关契约），SSE 顶层 event 行仅透传兼容
                    yield {"event": "", "data": payload}



class GatewayV2Dialect:
    """Gateway v2 方言翻译器：turn/1 envelope（``type`` 字段）→ RuntimeEvent。

    envelope 契约（Gateway SseEventForwarder 原样转发 cimicode turn/1 帧）：
    ``{kind, sid, inv, epoch, seq, type, data, rev?, eventID?}``。
    事件名取 envelope 的 ``type``（如 ``session.next.text.delta@`` 带版本
    命名）；终态 = ``invocation.idle / yielded / failed``（互斥且恰一次，
    终态帧后服务端主动关闭连接——这是正常结束，不是断流）。
    """

    name = "gateway-v2"

    # 终态 type 集合（turn/1 契约 §7）
    TERMINAL_TYPES = {"invocation.idle", "invocation.yielded", "invocation.failed"}

    def __init__(self) -> None:
        self.parts: dict[str, str] = {}      # part_id → 已累积文本（仅正文 text part）
        self.part_order: list[str] = []      # part 首次出现顺序（终态按此拼接）
        self._closed_parts: set[str] = set() # 已收口（ended）的 part，后续 delta 不再追加
        self._reasoning_parts: set[str] = set()  # reasoning part 的 id 集合——不进正文聚合
        self.terminal_seen = False          # 是否已见终态帧

    def is_terminal(self, event_type: str) -> bool:
        """终态判定（含 v0.1 遗留 done/error 兼容）。"""
        return event_type in self.TERMINAL_TYPES or event_type in {"done", "error"}

    def translate(self, raw_event: dict[str, Any]) -> list[RuntimeEvent]:
        """翻译单个 envelope 为 RuntimeEvent（列表包装保持接口统一）。"""
        envelope = raw_event.get("data", raw_event)
        if not isinstance(envelope, dict):
            envelope = {"value": envelope}
        event_type = str(envelope.get("type") or "")
        payload = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}

        # reasoning part 识别：delta 帧的事件名自带 reasoning；ended 帧
        # （message.part.updated / session.next.text.ended@1）的 part 对象
        # 带 type 字段（"reasoning" / "text"）——两种来源都标记，标记后
        # 不进 parts 聚合（正文只拼 text part，思考链不混入回复）。
        # part 身份以首见为准：已标记 reasoning 的 part，后续即使事件名
        # 不带 reasoning（平台方言差异）也不再进正文。
        is_reasoning = "reasoning" in event_type
        part_obj_pre = payload.get("part") if isinstance(payload.get("part"), dict) else {}
        # part id 提取：官方 schema（frame-structure-catalog §4.6-4.9）的
        # 字段名是 textID / reasoningID / callID（不是 partID）——实测
        # 2026-09-29 抓包确认。旧 partID/part_id 保留兼容（v0.1 方言）。
        # 不匹配时聚合缓冲拿不到 part 身份，text.ended 全文帧会被丢弃，
        # 订阅晚于 live delta 的场景（只回放 durable 帧）正文全部丢失。
        part_id_pre = str(
            payload.get("textID")
            or payload.get("reasoningID")
            or payload.get("callID")
            or payload.get("partID")
            or payload.get("part_id")
            or part_obj_pre.get("partID")
            or part_obj_pre.get("part_id")
            or part_obj_pre.get("id")
            or ""
        )
        if part_id_pre and part_id_pre in self._reasoning_parts:
            is_reasoning = True
        if event_type in {
            "session.next.text.delta",
            "session.next.reasoning.delta",
            "message.part.delta",
            "message",
        }:
            text = str(payload.get("delta") or payload.get("text") or "")
            kind = RuntimeEventKind.TEXT_DELTA
        elif event_type in {"session.next.text.ended@1", "session.next.reasoning.ended@1", "message.part.updated"}:
            # durable 全文收口帧：part 全量替换。reasoning.ended 也走
            # TEXT_DONE 路径（is_reasoning 已标记，不进正文聚合但登记
            # part 身份——后续同 part 的迟到 delta 不再混入正文）。
            part = payload.get("part") if isinstance(payload.get("part"), dict) else {}
            text = str(part.get("text") or payload.get("text") or "")
            kind = RuntimeEventKind.TEXT_DONE
            if str(part.get("type") or "") == "reasoning":
                is_reasoning = True
        elif event_type in {"invocation.idle", "invocation.yielded", "done"}:
            # 正常终态：done.content / idle 无正文时按 part 首现顺序拼接
            text = str(payload.get("content") or payload.get("text") or "")
            kind = RuntimeEventKind.TURN_COMPLETED
            self.terminal_seen = True
        elif event_type in {"invocation.failed", "error", "session.error"}:
            error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            text = str(error.get("message") or payload.get("message") or payload.get("reason") or "")
            kind = RuntimeEventKind.RUNTIME_ERROR
            self.terminal_seen = True
        elif event_type == "session.next.tool.called@1":
            text = str(payload.get("tool") or "")
            kind = RuntimeEventKind.TOOL_STARTED
        elif event_type in {"session.next.tool.success@1", "session.next.tool.failed@1"}:
            text = str(payload.get("output") or payload.get("error") or "")
            kind = RuntimeEventKind.TOOL_FINISHED
        elif event_type == "invocation.started":
            kind = RuntimeEventKind.TURN_STARTED
            text = ""
        else:
            # 未识别事件（turn.accepted / status.changed / diagnostic / live 增量等）：
            # 不产生 RuntimeEvent，原文已在 envelope 里可诊断，不丢失也不误报错
            return []

        # part 缓冲聚合：带 part_id 的事件按 delta 追加 / ended 全量替换；
        # ended 后该 part 已收口（durable 全文），后续 delta 不再追加。
        # part id 字段名与上方预检一致（官方 textID/reasoningID/callID +
        # 旧 partID 兼容）。
        part_obj = payload.get("part") if isinstance(payload.get("part"), dict) else {}
        part_id = str(
            payload.get("textID")
            or payload.get("reasoningID")
            or payload.get("callID")
            or payload.get("partID")
            or payload.get("part_id")
            or part_obj.get("partID")
            or part_obj.get("part_id")
            or part_obj.get("id")
            or ""
        )
        if part_id and kind in {RuntimeEventKind.TEXT_DELTA, RuntimeEventKind.TEXT_DONE}:
            if is_reasoning:
                # reasoning part：只登记不聚合——终态拼接（下方）只拼
                # self.parts 里的正文 part，思考链不会混进回复正文。
                self._reasoning_parts.add(part_id)
            else:
                if part_id not in self.parts:
                    self.part_order.append(part_id)
                if kind == RuntimeEventKind.TEXT_DONE:
                    self.parts[part_id] = str(text)
                    self._closed_parts.add(part_id)
                elif part_id not in self._closed_parts:
                    self.parts[part_id] = self.parts.get(part_id, "") + str(text)
        # 终态未带全文时：按 part 首现顺序拼接聚合文本
        if kind == RuntimeEventKind.TURN_COMPLETED and not text:
            text = "\n".join(self.parts[part_id] for part_id in self.part_order)
        if is_reasoning and kind in {RuntimeEventKind.TEXT_DELTA, RuntimeEventKind.TEXT_DONE}:
            # reasoning 增量/收口帧不产生正文事件——上层（app.py）会把
            # TEXT_DELTA 拼进 response_text，思考链一旦透出就会混进回复
            # 正文（实测：deepseek-v4-flash 的英文思考链拼在中文正文前）。
            # 原文保留在 envelope（data 字段）里可诊断，不丢失。
            return []
        return [
            RuntimeEvent(
                seq=envelope.get("seq"),
                kind=kind,
                text=str(text),
                data=envelope,
            )
        ]
