"""opencode runtime adapter：基于 opencode headless server（``opencode serve``）的 REST+轮询实现。

协议要点（stable opencode REST API，按 1.18.27 校准）：
  * 会话：    ``POST /session`` -> 会话对象（顶层 ``id``）；
             ``GET /session`` -> 列表
  * 消息：    ``POST /session/{id}/message``，body ``{"parts": [{"type":
             "text", "text": ...}]}`` —— 该调用会**阻塞到整轮结束**
             （或模型报错），因此需要独立的长超时；
             ``GET /session/{id}/message`` -> ``{info: {id, role, time:
             {created, completed?}, error?}, parts: [...]}`` 列表
  * 完成信号：完成的 assistant 消息带 ``info.time.completed``；失败的 turn
             带 ``info.error``（``error.data.message`` 是上游信息，如配额
             错误引发的循环重试）。SSE 存在但近期版本有可靠性问题——
             我们改用轮询。
  * 系统提示：opencode 从工作目录读 ``AGENTS.md``（与 sandbox pod 共享的
             volume）；每轮 turn 前把生成的 agent.md 经 sandbox helper 端点
             （``POST {helper_url}/agents-md``，body 为原始 markdown）推进去。

会话绑定：controller 可在 openclaw.json（``bridge.runtime.sessionId``）预创建
会话 ID。为空时 adapter 自管会话（首轮创建、后续复用、sandbox 重启后 404
则重建）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from cimicode_bridge.events import RuntimeEvent, RuntimeEventKind

logger = logging.getLogger(__name__)


class OpenCodeAdapter:
    """opencode headless server 的 RuntimeAdapter 实现（REST + 轮询，非流式）。"""

    name = "opencode"

    def __init__(
        self,
        base_url: str,
        *,
        helper_url: str = "",
        timeout_seconds: int = 600,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        # helper 默认与 base_url 同源（helper 与 opencode 同打在一个 sandbox 镜像的 :4097）
        self.helper_url = helper_url.rstrip("/") if helper_url else ""
        self.timeout_seconds = timeout_seconds        # 整轮超时（POST 阻塞 + 轮询共用）
        self.poll_interval_seconds = poll_interval_seconds  # 轮询间隔
        self._session_id = ""                         # 自管会话时缓存的会话 ID
        self._client: httpx.AsyncClient | None = None

    def capabilities(self):  # pragma: no cover - trivial
        """能力声明：支持会话销毁；不支持中断事件/产物/流式。"""
        from cimicode_bridge.runtime.base import RuntimeCapabilities

        return RuntimeCapabilities(
            supports_session_destroy=True,
            supports_interrupt_event=False,
            supports_artifact=False,
            supports_streaming=False,
        )

    def _http(self) -> httpx.AsyncClient:
        """懒创建共享的 httpx 客户端（关闭后自动重建）。"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout_seconds)
        return self._client

    async def health(self) -> bool:
        """健康检查：GET /session 返回 200 即认为服务在线。"""
        try:
            response = await self._http().get(f"{self.base_url}/session")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def close(self) -> None:
        """关闭底层 httpx 客户端（应用停机时调用）。"""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def _push_agent_md(self, agent_md: str) -> None:
        """把 agent.md 推到 sandbox helper（opencode 从共享目录读 AGENTS.md）。"""
        if not self.helper_url:
            raise RuntimeError("opencode adapter requires runtime.helper_url (sandbox AGENTS.md helper)")
        logger.info(
            "opencode action: push agent.md to sandbox helper=%s bytes=%d",
            self.helper_url,
            len(agent_md.encode("utf-8")),
        )
        response = await self._http().post(
            f"{self.helper_url}/agents-md",
            content=agent_md.encode("utf-8"),
            headers={"Content-Type": "text/plain; charset=utf-8"},
        )
        response.raise_for_status()

    async def _ensure_session(self, session_id: str) -> str:
        """确保会话可用：传入 ID 有效则复用；404（sandbox 重启）则重建。"""
        if not session_id:
            session_id = self._session_id
        if session_id:
            response = await self._http().get(f"{self.base_url}/session/{session_id}")
            if response.status_code == 200:
                self._session_id = session_id
                logger.info("opencode action: reuse session id=%s", session_id)
                return session_id
            logger.warning("opencode session %s vanished; recreating", session_id)
        response = await self._http().post(f"{self.base_url}/session", json={})
        response.raise_for_status()
        created = response.json()
        # 兼容顶层 id / sessionID / info.id 三种返回形态
        session_id = str(
            created.get("id")
            or created.get("sessionID")
            or (created.get("info") or {}).get("id")
            or ""
        )
        if not session_id:
            raise RuntimeError(f"opencode session creation returned no id: {created!r}")
        self._session_id = session_id
        logger.info("opencode action: created session id=%s", session_id)
        return session_id

    async def _messages(self, session_id: str) -> list[dict[str, Any]]:
        """拉取会话全部消息（兼容裸列表与 {data: [...]} 包装两种返回）。"""
        response = await self._http().get(f"{self.base_url}/session/{session_id}/message")
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            return payload["data"]
        return payload if isinstance(payload, list) else []

    @staticmethod
    def _last_assistant_id(messages: list[dict[str, Any]]) -> str:
        """找最近一条 assistant 消息的 ID（作为本轮回复的基线）。"""
        for message in reversed(messages):
            info = message.get("info") or message
            if str(info.get("role")) == "assistant":
                return str(info.get("id") or "")
        return ""

    @staticmethod
    def _completed_assistant_texts(messages: list[dict[str, Any]], baseline_id: str) -> list[str]:
        """基线之后已完成的 assistant 文本列表（按时间正序）。

        单个 opencode turn 可能产出多条 assistant 消息（agent 在工具调用间
        叙述进展）。只有最后一条是 turn 的回复；较早的是房间期望看到的
        中途进展。
        """
        texts: list[str] = []
        for message in messages:
            info = message.get("info") or message
            if str(info.get("role")) != "assistant":
                continue
            message_id = str(info.get("id") or "")
            if not message_id or message_id == baseline_id:
                continue
            if (info.get("time") or {}).get("completed") is None:
                continue
            text = OpenCodeAdapter._extract_text(message)
            if text.strip():
                texts.append(text)
        return texts

    @staticmethod
    def _extract_text(message: dict[str, Any]) -> str:
        """提取消息里的全部 text part 并以换行拼接。"""
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
        """轮询等待本轮完成：新 assistant 消息 completed 即返回结果事件。

        错误 -> RUNTIME_ERROR；超时 -> TURN_INTERRUPTED。
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_seconds
        while True:
            await asyncio.sleep(self.poll_interval_seconds)
            messages = await self._messages(session_id)
            for message in reversed(messages):
                info = message.get("info") or message
                if str(info.get("role")) != "assistant":
                    continue
                message_id = str(info.get("id") or "")
                if not message_id or message_id == baseline_id:
                    continue
                time_info = info.get("time") or {}
                if time_info.get("completed") is None:
                    continue
                error = info.get("error")
                if isinstance(error, dict):
                    data = error.get("data") or {}
                    detail = str(data.get("message") or error.get("name") or "opencode turn failed")
                    return RuntimeEvent(
                        kind=RuntimeEventKind.RUNTIME_ERROR,
                        text=f"opencode turn failed: {detail}",
                        data={"session_id": session_id, "message_id": message_id},
                    )
                text = self._extract_text(message)
                return RuntimeEvent(
                    kind=RuntimeEventKind.TURN_COMPLETED,
                    text=text,
                    data={"session_id": session_id, "message_id": message_id},
                )
            if loop.time() >= deadline:
                return RuntimeEvent(
                    kind=RuntimeEventKind.TURN_INTERRUPTED,
                    text="opencode turn timed out waiting for assistant completion",
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
    ) -> list[RuntimeEvent]:
        """执行一轮对话：推 agent.md -> 确保会话 -> POST 消息 -> 轮询结果。

        history 已由调用方折叠进 user_message（HistoryStore.build_context 的
        契约 §4 两段标记形态）；sandbox 绑定由 base_url 自身承载。
        """
        del history, sandbox_id
        turn_started = time.monotonic()
        try:
            await self._push_agent_md(agent_md)
            resolved = await self._ensure_session(session_id)
            baseline = self._last_assistant_id(await self._messages(resolved))
            logger.info(
                "opencode action: post message session=%s turn=%s user_message=%r",
                resolved,
                turn_id,
                user_message[:300],
            )
            response = await self._http().post(
                f"{self.base_url}/session/{resolved}/message",
                json={"parts": [{"type": "text", "text": user_message}]},
                # POST 在服务端阻塞到整轮结束，超时在 turn 超时基础上放宽 30s
                timeout=self.timeout_seconds + 30.0,
            )
            response.raise_for_status()
            completed = await self._poll_reply(resolved, baseline)
            if completed.kind == RuntimeEventKind.TURN_COMPLETED:
                # 中途叙述（工具调用间 agent 发的 Progress 更新）随最终回复
                # 一起带出——调用方会在回复前逐条转发，多步工作得以实时可见。
                texts = self._completed_assistant_texts(await self._messages(resolved), baseline)
                progress = [t for t in texts[:-1] if t.strip() and t.strip() != completed.text]
                if progress:
                    completed = RuntimeEvent(
                        kind=RuntimeEventKind.TURN_COMPLETED,
                        text=completed.text,
                        data={**(completed.data or {}), "progress_texts": progress},
                    )
            logger.info(
                "opencode action: turn finished session=%s turn=%s outcome=%s elapsed=%.1fs progress=%d reply=%r",
                resolved,
                turn_id,
                completed.kind.value,
                time.monotonic() - turn_started,
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
            logger.error("opencode HTTP error: %s %s", exc.response.status_code, exc.response.text[:400])
            return [RuntimeEvent(kind=RuntimeEventKind.RUNTIME_ERROR, text=f"opencode HTTP {exc.response.status_code}")]
        except httpx.TimeoutException as exc:
            # str(TimeoutException) 常为空——把含义说清楚：阻塞的 POST 放弃了，
            # 但 opencode turn 通常仍在服务端继续跑（会话状态完好，后续轮次可续）。
            logger.error(
                "opencode turn POST timed out after %.0fs (%s); the opencode "
                "turn may still be running server-side",
                time.monotonic() - turn_started,
                type(exc).__name__,
            )
            return [RuntimeEvent(
                kind=RuntimeEventKind.RUNTIME_ERROR,
                text=f"opencode turn timed out after {self.timeout_seconds}s "
                     "(task still running server-side; ask again to re-attach)",
            )]
        except (httpx.HTTPError, RuntimeError) as exc:
            logger.error("opencode adapter failure: %s", exc)
            return [RuntimeEvent(kind=RuntimeEventKind.RUNTIME_ERROR, text=f"opencode adapter failure: {exc}")]
