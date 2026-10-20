"""cimicode-pod adapter：operator 供给的 cimicode pod（opencode 换皮）对接形态。

内部 cimicode 是换皮的 opencode——pod 形态的调用契约按 opencode headless
server（``opencode serve``）的 REST API 走，与 stateless 的 SSE gateway
方言已分化，本文件是独立实现（不继承 CimicodeAdapter 传输基座）。

协议要点（按 opencode 1.18.27 与 cimicode 同型标定，详见
contract/adapter-contract.md）：
  * 会话：``POST /session`` → 会话对象（顶层 ``id``）；``GET /session`` → 列表
  * 消息：``POST /session/{id}/message`` body ``{"system": ..., "parts":
    [{"type": "text", "text": ...}]}``——服务端阻塞整 turn 才返回（独立长
    超时）；``GET /session/{id}/message`` → ``{info: {id, role, time: {created,
    completed?}, error?}, parts: [...]}`` 列表
  * 完成信号：完成的 assistant 消息带 ``info.time.completed``；失败 turn 带
    ``info.error``（``error.data.message`` 是上游报错原文）。SSE 在近期版本
    不可靠——轮询代替
  * 系统指令：**消息体 ``system`` 字段**（opencode/cimicode 原生通道，
    1.18.27 session/prompt.ts PromptInput.system 与 cimicode 同型）——随消息
    持久化在 User 消息上，LLM 调用时拼进 system prompt；历史消息转换不含
    该字段，只在当前 turn 生效，团队名单变化**当 turn 实时生效**。旧链路
    （helper POST /agents-md 落 cwd AGENTS.md）已随 pod 内 helper 整体退役

会话绑定：Worker CR 的 runtime.yaml bridge 段不预建会话（pod 形态免绑定）；
adapter 自持会话（首 turn 创建、之后复用、404 后重建——pod 重建自愈）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from cimicode_bridge.events import RuntimeEvent, RuntimeEventKind
from cimicode_bridge.runtime.base import RuntimeCapabilities

logger = logging.getLogger(__name__)


class CimicodePodAdapter:
    """pod 形态：调用 operator 供给的集群内 cimicode（opencode）服务。"""

    name = "cimicode-pod"

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: int = 600,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self._session_id = ""                             # adapter 自持的 opencode 会话
        self._client: httpx.AsyncClient | None = None     # 长连客户端（懒建）

    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(
            supports_session_destroy=True,
            supports_interrupt_event=False,
            supports_artifact=False,
            supports_streaming=False,                     # 轮询形态，turn 结束才出全文
        )

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout_seconds)
        return self._client

    async def health(self) -> bool:
        try:
            response = await self._http().get(f"{self.base_url}/session")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def _ensure_session(self, session_id: str) -> str:
        """会话自愈：入参（controller 投影，pod 形态恒空）或自持 id 可复用；
        404/空则重建（pod 重建后会话目录丢失是预期内场景）。"""
        if not session_id:
            session_id = self._session_id
        if session_id:
            response = await self._http().get(f"{self.base_url}/session/{session_id}")
            if response.status_code == 200:
                self._session_id = session_id
                logger.info("cimicode-pod action: reuse session id=%s", session_id)
                return session_id
            logger.warning("cimicode-pod session %s vanished; recreating", session_id)
        response = await self._http().post(f"{self.base_url}/session", json={})
        response.raise_for_status()
        created = response.json()
        session_id = str(
            created.get("id")
            or created.get("sessionID")
            or (created.get("info") or {}).get("id")
            or ""
        )
        if not session_id:
            raise RuntimeError(f"cimicode-pod session creation returned no id: {created!r}")
        self._session_id = session_id
        logger.info("cimicode-pod action: created session id=%s", session_id)
        return session_id

    async def _messages(self, session_id: str) -> list[dict[str, Any]]:
        response = await self._http().get(f"{self.base_url}/session/{session_id}/message")
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            return payload["data"]
        return payload if isinstance(payload, list) else []

    @staticmethod
    def _last_assistant_id(messages: list[dict[str, Any]]) -> str:
        for message in reversed(messages):
            info = message.get("info") or message
            if str(info.get("role")) == "assistant":
                return str(info.get("id") or "")
        return ""

    @staticmethod
    def _slice_after_baseline(
        messages: list[dict[str, Any]], baseline_id: str
    ) -> list[dict[str, Any]] | None:
        """baseline 之后（按列表位置）的消息切片；baseline_id 空返回全表。

        返回 None 表示 baseline_id 非空却不在列表中（session 状态异常）。
        消息列表按时间序——只按 id 排除单条会让历史回复混进本 turn 的
        扫描窗口（progress 回放 / 轮询捞到旧回复），必须按位置截断。
        """
        if not baseline_id:
            return messages
        for idx, message in enumerate(messages):
            if str((message.get("info") or message).get("id") or "") == baseline_id:
                return messages[idx + 1:]
        return None

    @staticmethod
    async def _abandon(post_task: "asyncio.Task[Any] | None") -> None:
        """放弃仍在跑的 POST 后台任务：取消并吞掉结果（调用方已另行定终态）。"""
        if post_task is None or post_task.done():
            return
        post_task.cancel()
        try:
            await post_task
        except asyncio.CancelledError:
            pass
        except BaseException:  # noqa: BLE001 — 放弃路径不关心失败原因，只防泄漏
            pass

    async def _post_turn(self, session_id: str, agent_md: str, user_message: str) -> Any:
        """提交 turn 消息（服务端阻塞整 turn 才返回）。HTTP 非 2xx 在任务内
        raise_for_status——异常经轮询循环的 post_task.exception() 快速上报，
        不会退化成"死等超时"。"""
        response = await self._http().post(
            f"{self.base_url}/session/{session_id}/message",
            json={"system": agent_md, "parts": [{"type": "text", "text": user_message}]},
            # POST 服务端阻塞整 turn——给独立长超时（轮询另有自己的窗口）
            timeout=self.timeout_seconds + 30.0,
        )
        response.raise_for_status()
        return response

    @staticmethod
    def _extract_text(message: dict[str, Any]) -> str:
        info = message.get("info") or message
        parts = info.get("parts") or message.get("parts") or []
        texts: list[str] = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text" and part.get("text"):
                texts.append(str(part["text"]))
        return "\n".join(texts)

    async def _poll_reply(
        self,
        session_id: str,
        baseline_id: str,
        on_event: Any | None = None,
        post_task: "asyncio.Task[Any] | None" = None,
    ) -> RuntimeEvent:
        """轮询直到 baseline 之后出现已完成的 assistant 消息（或超时）。

        POST 由调用方包成后台任务传入：阻塞 POST 期间并发轮询消息列表，
        turn 中途的 assistant 前置叙述通过 on_event 以 PROGRESS 事件实时
        透出（hold-last：最新一条持有不发——它可能是 turn 的终态回复；
        出现更新一条时才把前一条定性为过程叙述发出，终态回复永不重复）。
        on_event=None 时退回批量模式：不发实时事件，全部前置叙述随完成
        事件的 progress_texts 一次性透出（旧行为，兼容既有调用方/测试）。

        完成信号 = POST 已返回 且 baseline 后出现已完成 assistant 消息
        （POST 响应体不带完成语义，完成/失败信号在消息列表的
        info.time.completed / info.error 上）。

        按位置截断而非只排除 baseline 那一条 id：baseline 之前的同 session
        历史回复也是已完成 assistant——只排 id 会让每 turn 的 progress
        回放全部历史并随对话单调增长。baseline 非空却不在列表中（session
        状态异常）时本轮跳过——progress 宁可缺失也不回放历史。
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_seconds
        emitted = 0   # 已实时发出的前置叙述数（按位置计数，hold-last 基准）
        post_done = post_task is None
        while True:
            if post_task is not None and post_task.done():
                exc = post_task.exception()
                if exc is not None:
                    # POST 失败即报——turn 大概率未启动；文案与同步路径的
                    # 异常分支一一对应（HTTP 状态码 / 超时 / 其他故障）。
                    if isinstance(exc, httpx.HTTPStatusError):
                        logger.error(
                            "cimicode-pod HTTP error: %s %s",
                            exc.response.status_code, exc.response.text[:400],
                        )
                        return RuntimeEvent(
                            kind=RuntimeEventKind.RUNTIME_ERROR,
                            text=f"cimicode-pod HTTP {exc.response.status_code}",
                        )
                    if isinstance(exc, httpx.TimeoutException):
                        # 阻塞 POST 放弃 ≠ turn 死亡：opencode 侧通常仍在跑
                        # （会话状态完好，后续 turn 可继续）。文案带这层语义。
                        logger.error(
                            "cimicode-pod turn POST timed out after %.0fs; "
                            "the turn may still be running server-side", self.timeout_seconds,
                        )
                        return RuntimeEvent(kind=RuntimeEventKind.RUNTIME_ERROR, text=(
                            f"cimicode-pod turn timed out after {self.timeout_seconds}s "
                            "(task still running server-side; ask again to re-attach)"
                        ))
                    logger.error("cimicode-pod adapter failure: %s", exc)
                    return RuntimeEvent(
                        kind=RuntimeEventKind.RUNTIME_ERROR,
                        text=f"cimicode-pod adapter failure: {exc}",
                    )
                post_done = True
            await asyncio.sleep(self.poll_interval_seconds)
            fresh = self._slice_after_baseline(await self._messages(session_id), baseline_id)
            if fresh is None:
                # baseline 非空却不在列表里（session 状态异常）——本轮跳过，
                # 继续等 deadline 兜底；绝不扫全表（会把历史回复当新完成）。
                continue
            completed: list[tuple[str, str, dict[str, Any]]] = []  # (id, text, info) 时间序
            for message in fresh:
                info = message.get("info") or message
                if str(info.get("role")) != "assistant":
                    continue
                message_id = str(info.get("id") or "")
                if not message_id:
                    continue
                if (info.get("time") or {}).get("completed") is None:
                    continue
                completed.append((message_id, CimicodePodAdapter._extract_text(message), info))
            # hold-last 实时透出：最新一条持有（可能是终态回复），其余按
            # 位置逐条以 PROGRESS 发出；发出的内容不再进 progress_texts。
            while on_event is not None and len(completed) > emitted + 1:
                text = completed[emitted][1]
                if text.strip() and text.strip() != "NO_REPLY":
                    await on_event(RuntimeEvent(kind=RuntimeEventKind.PROGRESS, text=text))
                emitted += 1
            if completed:
                message_id, text, info = completed[-1]  # 最新一条定终态（与旧语义一致）
                error = info.get("error")
                if isinstance(error, dict):
                    await self._abandon(post_task)
                    data = error.get("data") or {}
                    detail = str(data.get("message") or error.get("name") or "cimicode-pod turn failed")
                    return RuntimeEvent(
                        kind=RuntimeEventKind.RUNTIME_ERROR,
                        text=f"cimicode-pod turn failed: {detail}",
                        data={"session_id": session_id, "message_id": message_id},
                    )
                if post_done:
                    # 余量兜底：实时模式漏发的（及 on_event=None 批量模式的
                    # 全部）前置叙述随完成事件补发；过滤口径与旧批量一致。
                    texts = [t for _, t, _ in completed]
                    remainder = [
                        t for t in texts[emitted:-1]
                        if t.strip() and t.strip() != "NO_REPLY" and t.strip() != text
                    ]
                    result_data: dict[str, Any] = {"session_id": session_id, "message_id": message_id}
                    if remainder:
                        result_data["progress_texts"] = remainder
                    return RuntimeEvent(
                        kind=RuntimeEventKind.TURN_COMPLETED,
                        text=text,
                        data=result_data,
                    )
                # 最新完成消息已出现但 POST 未返回——turn 可能仍在续写，
                # 继续轮询直到 POST 收口（终态判定以 POST 返回为准）。
            if loop.time() >= deadline:
                await self._abandon(post_task)
                return RuntimeEvent(
                    kind=RuntimeEventKind.TURN_INTERRUPTED,
                    text="cimicode-pod turn timed out waiting for assistant completion",
                    data={"session_id": session_id},
                )

    async def chat(
        self,
        *,
        session_id: str,
        agent_md: str,
        user_message: str,
        turn_id: str = "",   # 可选日志关联（Matrix event_id；Gateway v2 下 turnId 由平台生成）
        on_event: Any | None = None,  # async on_event(RuntimeEvent)：轮询期间以 PROGRESS 实时透出 turn 中途叙述（None=批量模式，随完成事件补发）
    ) -> list[RuntimeEvent]:
        """提交 turn：会话自愈 → 阻塞 POST 后台跑（agent.md 走消息体 system
        字段）+ 并发轮询（前置叙述实时透出，完成信号在消息列表上）。

        三段式上下文已由调用方折进 user_message（契约 §4）；
        sandbox 绑定由 base_url 本身承载（单 pod 无独立沙箱）。
        agent_md 经 POST body 的 ``system`` 字段随消息下发——opencode/cimicode
        原生通道，当前 turn 即生效（历史消息转换不含该字段，无重复注入）。
        正常完成返回 [TEXT_DELTA(全文), TURN_COMPLETED]——与 stateless 的
        事件形态同构（聚合器按 turn_completed 收口，全文发一次）。
        """
        turn_started = time.monotonic()
        try:
            resolved = await self._ensure_session(session_id)
            baseline = self._last_assistant_id(await self._messages(resolved))
            logger.info(
                "cimicode-pod action: post message session=%s turn=%s system_bytes=%d user_message=%r",
                resolved, turn_id, len(agent_md.encode("utf-8")), user_message[:300],
            )
            # 阻塞 POST 转后台任务：轮询与 POST 并发进行，turn 中途的
            # assistant 前置叙述经 on_event 实时发出（PROGRESS），房间内
            # 可见顺序与工具的即时副作用（如 delegate-commit 的派单通知）
            # 保持一致——旧路径 POST 跑完才补发全部 progress，会把顺序
            # 颠倒成"派单先于立项/规划"。
            post_task: asyncio.Task[Any] = asyncio.create_task(
                self._post_turn(resolved, agent_md, user_message)
            )
            try:
                completed = await self._poll_reply(resolved, baseline, on_event=on_event, post_task=post_task)
            except BaseException:
                await self._abandon(post_task)
                raise
            logger.info(
                "cimicode-pod action: turn finished session=%s turn=%s outcome=%s elapsed=%.1fs progress=%d reply=%r",
                resolved, turn_id, completed.kind.value, time.monotonic() - turn_started,
                len((completed.data or {}).get("progress_texts") or []),
                (completed.text or "")[:300],
            )
            if completed.kind == RuntimeEventKind.TURN_COMPLETED:
                return [
                    RuntimeEvent(kind=RuntimeEventKind.TEXT_DELTA, text=completed.text),
                    completed,
                ]
            return [completed]
        except httpx.HTTPStatusError as exc:
            logger.error("cimicode-pod HTTP error: %s %s", exc.response.status_code, exc.response.text[:400])
            return [RuntimeEvent(kind=RuntimeEventKind.RUNTIME_ERROR, text=f"cimicode-pod HTTP {exc.response.status_code}")]
        except httpx.TimeoutException:
            # 阻塞 POST 放弃 ≠ turn 死亡：opencode 侧通常仍在跑（会话状态
            # 完好，后续 turn 可继续）。报错文案带上这层语义。
            logger.error(
                "cimicode-pod turn POST timed out after %.0fs; the turn may still be running server-side",
                time.monotonic() - turn_started,
            )
            return [RuntimeEvent(
                kind=RuntimeEventKind.RUNTIME_ERROR,
                text=f"cimicode-pod turn timed out after {self.timeout_seconds}s "
                     "(task still running server-side; ask again to re-attach)",
            )]
        except (httpx.HTTPError, RuntimeError) as exc:
            logger.error("cimicode-pod adapter failure: %s", exc)
            return [RuntimeEvent(kind=RuntimeEventKind.RUNTIME_ERROR, text=f"cimicode-pod adapter failure: {exc}")]
