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
    def _completed_assistant_texts(messages: list[dict[str, Any]], baseline_id: str) -> list[str]:
        """baseline 之后（按列表位置）已完成的 assistant 文本（按时间序）。

        一个 opencode turn 可能产出多条 assistant 消息（工具调用之间穿插
        进度叙述）；最后一条是 turn 的正式回复，之前的按 progress_texts
        透出给房间。

        必须按位置截断而非只排除 baseline 那一条 id：baseline 之前的同
        session 历史回复也是已完成 assistant——只排 id 会让每 turn 的
        progress 回放全部历史并随对话单调增长（progress=0,1,2,3...）。
        baseline_id 为空（新 session 无 assistant）时收集全部；非空却在
        列表中找不到时保守返回空——progress 宁可缺失也不回放历史。
        """
        if baseline_id:
            messages = CimicodePodAdapter._slice_after_baseline(messages, baseline_id) or []
        texts: list[str] = []
        for message in messages:
            info = message.get("info") or message
            if str(info.get("role")) != "assistant":
                continue
            message_id = str(info.get("id") or "")
            if not message_id:
                continue
            if (info.get("time") or {}).get("completed") is None:
                continue
            text = CimicodePodAdapter._extract_text(message)
            if text.strip():
                texts.append(text)
        return texts

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

    async def _poll_reply(self, session_id: str, baseline_id: str) -> RuntimeEvent:
        """轮询直到 baseline 之后出现已完成的 assistant 消息（或超时）。

        阻塞 POST 返回后仍需轮询：POST 的响应体不带完成语义，完成/失败
        信号在消息列表的 info.time.completed / info.error 上。
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_seconds
        while True:
            await asyncio.sleep(self.poll_interval_seconds)
            fresh = self._slice_after_baseline(await self._messages(session_id), baseline_id)
            if fresh is None:
                # baseline 非空却不在列表里（session 状态异常）——本轮跳过，
                # 继续等 deadline 兜底；绝不扫全表（会把历史回复当新完成）。
                continue
            for message in reversed(fresh):
                info = message.get("info") or message
                if str(info.get("role")) != "assistant":
                    continue
                message_id = str(info.get("id") or "")
                if not message_id:
                    continue
                time_info = info.get("time") or {}
                if time_info.get("completed") is None:
                    continue
                error = info.get("error")
                if isinstance(error, dict):
                    data = error.get("data") or {}
                    detail = str(data.get("message") or error.get("name") or "cimicode-pod turn failed")
                    return RuntimeEvent(
                        kind=RuntimeEventKind.RUNTIME_ERROR,
                        text=f"cimicode-pod turn failed: {detail}",
                        data={"session_id": session_id, "message_id": message_id},
                    )
                text = CimicodePodAdapter._extract_text(message)
                return RuntimeEvent(
                    kind=RuntimeEventKind.TURN_COMPLETED,
                    text=text,
                    data={"session_id": session_id, "message_id": message_id},
                )
            if loop.time() >= deadline:
                return RuntimeEvent(
                    kind=RuntimeEventKind.TURN_INTERRUPTED,
                    text="cimicode-pod turn timed out waiting for assistant completion",
                    data={"session_id": session_id},
                )

    async def chat(
        self,
        *,
        session_id: str,
        sandbox_id: str,
        turn_id: str,
        agent_md: str,
        history: list[dict[str, Any]],
        user_message: str,
        eid: str = "",
        extra_params: dict[str, str] | None = None,
    ) -> list[RuntimeEvent]:
        """提交 turn：会话自愈 → 阻塞 POST（agent.md 走消息体 system 字段）→ 轮询完成。

        history 已由调用方折进 user_message（三段式上下文，契约 §4）；
        sandbox 绑定由 base_url 本身承载（单 pod 无独立沙箱）。eid /
        extra_params 是 stateless 平台的参数（用户身份 / 追加键透传袋）——
        pod 形态无对应通道，收下忽略（TurnRunner 统一传参，两种 adapter
        签名同构）。
        agent_md 经 POST body 的 ``system`` 字段随消息下发——opencode/cimicode
        原生通道，当前 turn 即生效（历史消息转换不含该字段，无重复注入）。
        正常完成返回 [TEXT_DELTA(全文), TURN_COMPLETED]——与 stateless 的
        事件形态同构（聚合器按 turn_completed 收口，全文发一次）。
        """
        del history, sandbox_id, eid, extra_params
        turn_started = time.monotonic()
        try:
            resolved = await self._ensure_session(session_id)
            baseline = self._last_assistant_id(await self._messages(resolved))
            logger.info(
                "cimicode-pod action: post message session=%s turn=%s system_bytes=%d user_message=%r",
                resolved, turn_id, len(agent_md.encode("utf-8")), user_message[:300],
            )
            response = await self._http().post(
                f"{self.base_url}/session/{resolved}/message",
                json={"system": agent_md, "parts": [{"type": "text", "text": user_message}]},
                # POST 服务端阻塞整 turn——给独立长超时（轮询另有自己的窗口）
                timeout=self.timeout_seconds + 30.0,
            )
            response.raise_for_status()
            completed = await self._poll_reply(resolved, baseline)
            if completed.kind == RuntimeEventKind.TURN_COMPLETED:
                # 把 turn 中途的 assistant 进度叙述（工具调用之间的插话）
                # 随完成事件透出——调用方先发 progress 再发正式回复。
                texts = self._completed_assistant_texts(await self._messages(resolved), baseline)
                progress = [t for t in texts[:-1] if t.strip() and t.strip() != completed.text]
                if progress:
                    completed = RuntimeEvent(
                        kind=RuntimeEventKind.TURN_COMPLETED,
                        text=completed.text,
                        data={**(completed.data or {}), "progress_texts": progress},
                    )
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
