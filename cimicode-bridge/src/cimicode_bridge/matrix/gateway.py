"""Matrix 传输层：matrix-nio 客户端封装（whoami/sync 循环/收发/typing/401 刷新）。"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from nio import AsyncClient, AsyncClientConfig, RoomMessageText, WhoamiResponse

from cimicode_bridge.render import render_matrix_message
from cimicode_bridge.store.base import StateStore

logger = logging.getLogger(__name__)
MessageHandler = Callable[[str, str, str, dict[str, Any]], Awaitable[None]]

# 401 token 刷新重试参数（对齐 CoPaw）
MAX_TOKEN_REFRESH_RETRIES = 3
TOKEN_REFRESH_BACKOFF_S = 5


class MatrixGateway:
    """matrix-nio 传输封装，把 Matrix 协议细节隔离在 app.py 之外。"""

    def __init__(
        self,
        homeserver: str,
        access_token: str,
        *,
        sync_timeout_seconds: int = 30,
        on_message: MessageHandler | None = None,
        state_store: StateStore | None = None,
        since_key: str = "matrix:since",
        refresh_token: Callable[[], Awaitable[str | None]] | None = None,
    ) -> None:
        # store_sync_tokens=False：since 由 bridge 自己管理（内存/StateStore），不交给 SDK
        self.client = AsyncClient(
            homeserver,
            user="",
            config=AsyncClientConfig(store_sync_tokens=False),
        )
        self.client.access_token = access_token
        self.sync_timeout_seconds = sync_timeout_seconds   # 长轮询超时（秒）
        self.on_message = on_message                       # 文本消息回调（room,sender,event_id,content）
        self.user_id: str | None = None                    # whoami 后回填
        self.connected = False
        self._stopped = asyncio.Event()
        self.since: str | None = None                      # 当前 sync 游标
        self.state_store = state_store                     # since 持久化后端（可选）
        self.since_key = since_key
        self.refresh_token = refresh_token                 # 401 刷新回调（可选）
        self._typing_tasks: dict[str, asyncio.Task[None]] = {}  # room_id → 续期任务

    async def authenticate(self) -> bool:
        """token 校验：whoami 拿自身 user_id/device_id（失败返回 False）。"""
        response = await self.client.whoami()
        if not isinstance(response, WhoamiResponse):
            logger.warning("Matrix whoami failed: %s", response)
            return False
        self.user_id = response.user_id
        self.client.user_id = response.user_id
        self.client.user = response.user_id
        if response.device_id:
            self.client.device_id = response.device_id
        return True

    async def start(self) -> None:
        """启动：whoami → 注册文本回调 → 手动 sync 循环（since 持久化 + 退避 + 401 刷新）。"""
        if not await self.authenticate():
            return
        self.client.add_event_callback(self._on_text, RoomMessageText)
        self.connected = True
        self._stopped.clear()
        # 从 StateStore 恢复 since（无则首次全量）
        if self.state_store is not None:
            self.since = await self.state_store.get(self.since_key)
        backoff = 1.0
        try:
            while not self._stopped.is_set():
                try:
                    response = await self.client.sync(
                        timeout=self.sync_timeout_seconds * 1000,
                        full_state=self.since is None,  # 首次（无 since）才拉全量状态
                        since=self.since,
                    )
                    # 每轮保存 next_batch（内存 + StateStore）
                    next_batch = getattr(response, "next_batch", None)
                    if next_batch:
                        self.since = next_batch
                        if self.state_store is not None:
                            await self.state_store.set(self.since_key, next_batch)
                    backoff = 1.0  # 成功即重置退避
                    self.connected = True
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.connected = False
                    # 401/M_UNKNOWN_TOKEN：走刷新链（3 次 × 5s），成功重认证后继续
                    err_str = str(exc)
                    if "M_UNKNOWN_TOKEN" in err_str or "401" in err_str:
                        logger.warning("Matrix sync received 401, attempting token refresh")
                        refreshed = False
                        if self.refresh_token is not None:
                            for _attempt in range(MAX_TOKEN_REFRESH_RETRIES):
                                token = await self.refresh_token()
                                if token:
                                    self.client.access_token = token
                                    if await self.authenticate():
                                        refreshed = True
                                        break
                                await asyncio.sleep(TOKEN_REFRESH_BACKOFF_S)
                        if not refreshed:
                            logger.error("Matrix token refresh exhausted, sync will keep retrying")
                        continue
                    # 其他错误：指数退避 1s → 30s
                    logger.warning("Matrix sync failed; retrying in %.1fs: %s", backoff, exc)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
        except asyncio.CancelledError:
            raise
        finally:
            self.connected = False

    async def _on_text(self, room: Any, event: RoomMessageText) -> None:
        """文本事件回调：忽略自发消息，把 content 原样交给上层处理链。"""
        if event.sender == self.user_id or self.on_message is None:
            return
        content = getattr(event, "source", {}).get("content", {}) or {}
        await self.on_message(
            room.room_id,
            event.sender,
            event.event_id,
            {
                "body": event.body or "",
                **content,
            },
        )

    async def send_text(self, room_id: str, body: str, *, content: dict[str, Any] | None = None) -> str | None:
        """发送 m.room.message（markdown 渲染 + 三层 mention），返回 event_id。"""
        message = render_matrix_message(body)
        if content:
            message.update(content)
        response = await self.client.room_send(
            room_id,
            "m.room.message",
            message,
            ignore_unverified_devices=True,
        )
        event_id = getattr(response, "event_id", None)
        return str(event_id) if event_id else None

    # ------------------------------------------------------------------
    # Typing 指示器（对齐 CoPaw：服务端 30s 超时 → 25s 续期）
    # ------------------------------------------------------------------
    async def start_typing(self, room_id: str) -> None:
        """开启 typing 并启动后台续期任务（长 turn 期间保持指示）。"""
        await self._typing_once(room_id, True)
        self._stop_typing(room_id)
        task = asyncio.create_task(self._typing_renewal_loop(room_id))
        self._typing_tasks[room_id] = task

    async def stop_typing(self, room_id: str) -> None:
        """停止 typing：取消续期任务并发送 typing=false。"""
        self._stop_typing(room_id)
        await self._typing_once(room_id, False)

    async def _typing_once(self, room_id: str, typing: bool) -> None:
        """单次 typing 置位（失败仅 debug，不影响主流程）。"""
        try:
            await self.client.room_typing(room_id, typing_state=typing, timeout=30000)
        except Exception as exc:
            logger.debug("room_typing(%s, %s) failed: %s", room_id, typing, exc)

    async def _typing_renewal_loop(self, room_id: str) -> None:
        """每 25 秒续期一次 typing（服务端 30s 超时）。"""
        try:
            while True:
                await asyncio.sleep(25)
                await self._typing_once(room_id, True)
        except asyncio.CancelledError:
            raise

    def _stop_typing(self, room_id: str) -> None:
        """取消并移除该房间的续期任务。"""
        task = self._typing_tasks.pop(room_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def stop(self) -> None:
        """停机：停止 sync 循环、取消全部 typing 任务、关闭客户端。"""
        self._stopped.set()
        self.connected = False
        for task in list(self._typing_tasks.values()):
            task.cancel()
        self._typing_tasks.clear()
        await self.client.close()