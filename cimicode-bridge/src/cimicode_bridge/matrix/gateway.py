from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from nio import AsyncClient, AsyncClientConfig, RoomMessageText, WhoamiResponse

from cimicode_bridge.render import render_matrix_message
from cimicode_bridge.store.base import StateStore

logger = logging.getLogger(__name__)
MessageHandler = Callable[[str, str, str, dict[str, Any]], Awaitable[None]]

MAX_TOKEN_REFRESH_RETRIES = 3
TOKEN_REFRESH_BACKOFF_S = 5

# @localpart with optional :domain — Matrix user identifiers. The bare-local
# form is what LLM replies naturally produce ("TASK_COMPLETED ... @oct-lead"),
# but copaw's group-room requireMention check only accepts the full MXID in
# plain text; structured m.mentions.user_ids is its first-class signal.
# Localpart stays permissive (room member index validates every hit, so prose
# like "path@host" never fabricates a mention); the domain is strictly ASCII
# per the server-name grammar so "任务:说明" is not parsed as a user id.
MENTION_TOKEN_RE = re.compile(r"@([^\s@:，。,；;()（）【】\[\]\"']+)(?::([A-Za-z0-9.\-]+))?")
ROOM_MEMBERS_TTL_S = 600.0


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
        on_authenticated: Callable[[str], None] | None = None,
    ) -> None:
        self.client = AsyncClient(
            homeserver,
            user="",
            config=AsyncClientConfig(store_sync_tokens=False),
        )
        self.client.access_token = access_token
        self.sync_timeout_seconds = sync_timeout_seconds
        self.on_message = on_message
        self.on_authenticated = on_authenticated
        self.user_id: str | None = None
        self.connected = False
        self._stopped = asyncio.Event()
        self.since: str | None = None
        self.state_store = state_store
        self.since_key = since_key
        self.refresh_token = refresh_token
        self._typing_tasks: dict[str, asyncio.Task[None]] = {}
        # room_id -> (expires_at_monotonic, alias -> full MXID). Feeds the
        # outbound m.mentions mapping so "@oct-lead" in a reply reaches the
        # leader even though its plain-text fallback needs the full MXID.
        self._room_members: dict[str, tuple[float, dict[str, str]]] = {}

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
        # Notify the app BEFORE the first sync: initial-sync timeline events
        # are dispatched to callbacks immediately, so consumers like the
        # mention filter must know this identity by then (setting it from
        # the app's startup poll loop races the first dispatched event).
        if self.on_authenticated is not None:
            self.on_authenticated(response.user_id)
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
                    # Accept pending room invites: the controller invites the
                    # worker identity to team rooms, but nothing joins on our
                    # behalf — without this the sync loop never sees their
                    # messages.
                    for invite_room_id in list(getattr(getattr(response, "rooms", None), "invite", {}) or {}):
                        try:
                            join_resp = await self.client.join(invite_room_id)
                            if getattr(join_resp, "room_id", None) == invite_room_id:
                                logger.info("joined invited room %s", invite_room_id)
                            else:
                                logger.warning("join response for invited room %s: %s", invite_room_id, join_resp)
                        except Exception as exc:
                            logger.warning("failed to join invited room %s: %s", invite_room_id, exc)
                    next_batch = getattr(response, "next_batch", None)
                    if next_batch and self.since is None:
                        logger.info(
                            "matrix sync established user=%s rooms=%s",
                            self.user_id,
                            len(getattr(getattr(response, "rooms", None), "join", {}) or {}),
                        )
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
        if "m.mentions" not in message:
            mention_ids = await self._resolve_mentions(room_id, body)
            if mention_ids:
                message["m.mentions"] = {"user_ids": mention_ids}
                logger.info("reply mentions room=%s users=%s", room_id, mention_ids)
        response = await self.client.room_send(
            room_id,
            "m.room.message",
            message,
            ignore_unverified_devices=True,
        )
        event_id = getattr(response, "event_id", None)
        return str(event_id) if event_id else None

    async def _room_member_index(self, room_id: str) -> dict[str, str] | None:
        """alias (lowercased localpart/display name) -> full MXID, TTL cached.

        Returns None when the joined-members lookup fails so callers can
        degrade (full MXIDs in the body still pass through unverified).
        """
        cached = self._room_members.get(room_id)
        now = time.monotonic()
        if cached and cached[0] > now:
            return cached[1]
        try:
            response = await self.client.joined_members(room_id)
            members = getattr(response, "members", None)
            if members is None:
                return cached[1] if cached else None
            index: dict[str, str] = {}
            for member in members:
                mxid = str(getattr(member, "user_id", "") or "")
                if not mxid.startswith("@") or ":" not in mxid:
                    continue
                index[mxid[1:].split(":", 1)[0].lower()] = mxid
                display = str(getattr(member, "display_name", "") or "").strip().lower()
                if display:
                    index.setdefault(display, mxid)
            self._room_members[room_id] = (now + ROOM_MEMBERS_TTL_S, index)
            return index
        except Exception as exc:
            logger.debug("joined_members(%s) failed: %s", room_id, exc)
            return cached[1] if cached else None

    async def _resolve_mentions(self, room_id: str, body: str) -> list[str]:
        """Full MXIDs for @tokens in the body (deduped, self excluded).

        Bare localparts resolve through the room member index; a token that
        already carries a domain is accepted verbatim only when the member
        index is unavailable or confirms it, so prose like "path@host" never
        fabricates a mention.
        """
        index = await self._room_member_index(room_id)
        resolved: list[str] = []
        for match in MENTION_TOKEN_RE.finditer(body):
            local, domain = match.group(1), match.group(2)
            if domain:
                candidate = f"@{local}:{domain}"
                if index is not None and candidate not in index.values():
                    continue
            else:
                if index is None:
                    continue
                candidate = index.get(local.lower(), "")
                if not candidate:
                    continue
            if candidate != self.user_id and candidate not in resolved:
                resolved.append(candidate)
        return resolved

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