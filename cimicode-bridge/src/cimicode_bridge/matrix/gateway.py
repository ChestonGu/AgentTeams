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

MAX_TOKEN_REFRESH_RETRIES = 3
TOKEN_REFRESH_BACKOFF_S = 5


class MatrixGateway:
    """Small matrix-nio transport, keeping Matrix protocol out of app.py."""

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
        self.client = AsyncClient(
            homeserver,
            user="",
            config=AsyncClientConfig(store_sync_tokens=False),
        )
        self.client.access_token = access_token
        self.sync_timeout_seconds = sync_timeout_seconds
        self.on_message = on_message
        self.user_id: str | None = None
        self.connected = False
        self._stopped = asyncio.Event()
        self.since: str | None = None
        self.state_store = state_store
        self.since_key = since_key
        self.refresh_token = refresh_token
        self._typing_tasks: dict[str, asyncio.Task[None]] = {}

    async def authenticate(self) -> bool:
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
        if not await self.authenticate():
            return
        self.client.add_event_callback(self._on_text, RoomMessageText)
        self.connected = True
        self._stopped.clear()
        if self.state_store is not None:
            self.since = await self.state_store.get(self.since_key)
        backoff = 1.0
        try:
            while not self._stopped.is_set():
                try:
                    response = await self.client.sync(
                        timeout=self.sync_timeout_seconds * 1000,
                        full_state=self.since is None,
                        since=self.since,
                    )
                    next_batch = getattr(response, "next_batch", None)
                    if next_batch:
                        self.since = next_batch
                        if self.state_store is not None:
                            await self.state_store.set(self.since_key, next_batch)
                    backoff = 1.0
                    self.connected = True
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.connected = False
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
                    logger.warning("Matrix sync failed; retrying in %.1fs: %s", backoff, exc)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
        except asyncio.CancelledError:
            raise
        finally:
            self.connected = False

    async def _on_text(self, room: Any, event: RoomMessageText) -> None:
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
    # Typing indicator（对齐 CoPaw TYPING_* 参数：服务端 30s 超时，25s 续期）
    # ------------------------------------------------------------------
    async def start_typing(self, room_id: str) -> None:
        """开启 typing 并启动后台续期任务（长任务期间保持指示器）。"""
        await self._typing_once(room_id, True)
        self._stop_typing(room_id)
        task = asyncio.create_task(self._typing_renewal_loop(room_id))
        self._typing_tasks[room_id] = task

    async def stop_typing(self, room_id: str) -> None:
        """停止 typing：取消续期任务并发送 typing=false。"""
        self._stop_typing(room_id)
        await self._typing_once(room_id, False)

    async def _typing_once(self, room_id: str, typing: bool) -> None:
        try:
            await self.client.room_typing(room_id, typing_state=typing, timeout=30000)
        except Exception as exc:
            logger.debug("room_typing(%s, %s) failed: %s", room_id, typing, exc)

    async def _typing_renewal_loop(self, room_id: str) -> None:
        try:
            while True:
                await asyncio.sleep(25)
                await self._typing_once(room_id, True)
        except asyncio.CancelledError:
            raise

    def _stop_typing(self, room_id: str) -> None:
        task = self._typing_tasks.pop(room_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def stop(self) -> None:
        self._stopped.set()
        self.connected = False
        for task in list(self._typing_tasks.values()):
            task.cancel()
        self._typing_tasks.clear()
        await self.client.close()