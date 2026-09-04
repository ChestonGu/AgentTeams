from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import os
import httpx
from pathlib import Path
from typing import Any

from contextlib import asynccontextmanager
from fastapi import FastAPI

from cimicode_bridge.bootstrap import S3Bootstrap, WorkerBootstrapConfig
from cimicode_bridge.config import BridgeConfig, load_config
from cimicode_bridge.matrix_client import MentionFilter, RoleResolver
from cimicode_bridge.matrix.gateway import MatrixGateway
from cimicode_bridge.prompt import GenerateAgentMdError, build_agent_md_via_generator
from cimicode_bridge.render import build_agent_md
from cimicode_bridge.runtime.registry import build_runtime_adapter
from cimicode_bridge.session import HistoryStore, SessionManager
from cimicode_bridge.store.file import FileStore
from cimicode_bridge.store.memory import MemoryStore
from cimicode_bridge.store.redis import RedisStore

logger = logging.getLogger(__name__)


@dataclass
class BridgeApp:
    config_path: str = "config/bridge.example.yaml"
    debug: bool = False
    phase: str = "bootstrap"
    matrix_connected: bool = False
    runtime_healthy: bool = False
    ready: bool = False
    config: BridgeConfig | None = field(default=None, init=False)
    worker_files: WorkerBootstrapConfig | None = field(default=None, init=False)
    matrix_access_token: str = field(default="", init=False, repr=False)
    session_manager: SessionManager = field(default_factory=SessionManager)
    history_stores: dict[str, HistoryStore] = field(default_factory=dict)
    mention_filter: MentionFilter = field(default_factory=MentionFilter)
    matrix_gateway: MatrixGateway | None = field(default=None, init=False)
    runtime_client: Any | None = field(default=None, init=False)
    matrix_task: asyncio.Task[None] | None = field(default=None, init=False)
    state_store: Any | None = field(default=None, init=False)

    def start(self) -> None:
        # Root-level config: without this the cimicode_bridge.* loggers
        # inherit WARNING and every INFO line (sync established, message
        # decisions) disappears while uvicorn's own access log stays visible.
        logging.basicConfig(level=os.getenv("BRIDGE_LOG_LEVEL", "INFO"))
        config_path = Path(self.config_path)
        self.config = load_config(config_path)
        self._apply_env_overrides()
        bootstrap = S3Bootstrap.from_environment()
        if bootstrap is not None:
            self.worker_files = bootstrap.load(retries=6, retry_interval_seconds=5)
            if self.worker_files is not None:
                self.matrix_access_token = self.worker_files.matrix_access_token
        if not self.matrix_access_token:
            self.matrix_access_token = os.getenv(self.config.matrix.token_env, "")
        if self.worker_files is not None:
            runtime = self.worker_files.bridge_runtime_config
            self.config.runtime.base_url = str(runtime.get("baseUrl") or runtime.get("base_url") or self.config.runtime.base_url)
            self.config.runtime.template_id = str(runtime.get("templateId") or runtime.get("template_id") or self.config.runtime.template_id)
            self.config.runtime.helper_url = self.worker_files.runtime_helper_url or self.config.runtime.helper_url
            self.config.runtime.session_id = self.worker_files.gateway_session_id
            self.config.runtime.sandbox_id = self.worker_files.gateway_sandbox_id
        self.mention_filter = MentionFilter(
            require_mention=self.config.filter.require_mention,
            allow_unknown=self.config.filter.allow_unknown,
            allowed_roles=set(self.config.filter.group_allow_from_worker),
            user_id=os.getenv("AGENTTEAMS_WORKER_MATRIX_USER_ID"),
            role_resolver=RoleResolver(
                self_user_id=os.getenv("AGENTTEAMS_WORKER_MATRIX_USER_ID"),
                leader=os.getenv("COORDINATION_LEADER"),
                admin=os.getenv("COORDINATION_ADMIN"),
                workers=set(filter(None, os.getenv("COORDINATION_WORKERS", "").split(","))),
            ),
        )
        self.phase = "bootstrap"
        # Runtime SPI factory: cimicode (SSE gateway) / opencode (REST+poll).
        # The adapter choice also decides the session-binding contract below.
        self.runtime_client = build_runtime_adapter(self.config.runtime)
        self.state_store = self._build_state_store()
        matrix_config = self.worker_files.matrix_config if self.worker_files else {}
        homeserver = str(matrix_config.get("homeserver") or self.config.matrix.homeserver_url)
        if homeserver.startswith("${"):
            homeserver = os.getenv("AGENTTEAMS_MATRIX_URL", "")
        # Only the cimicode adapter requires a pre-created gateway session id
        # (openclaw.json bridge.runtime.sessionId); the opencode adapter owns
        # its session lifecycle itself.
        session_required = self.config.runtime.adapter == "cimicode"
        if homeserver and self.matrix_access_token and (self.config.runtime.session_id or not session_required):
            self.matrix_gateway = MatrixGateway(
                homeserver,
                self.matrix_access_token,
                sync_timeout_seconds=self.config.matrix.sync_timeout_seconds,
                on_message=self.handle_matrix_message,
                state_store=self.state_store,
                since_key=f"matrix:since:{os.getenv('AGENTTEAMS_WORKER_NAME', 'worker')}",
                refresh_token=self._refresh_matrix_token,
                on_authenticated=self._on_matrix_authenticated,
            )
        self.runtime_healthy = False
        self.ready = False
        if self.debug:
            print(f"Loaded bridge config from {config_path}")
        print(f"Bridge started with runtime adapter: {self.config.runtime.adapter}")

    def _on_matrix_authenticated(self, user_id: str) -> None:
        """Feed the whoami-resolved identity into the mention filter.

        Called from the gateway right after authentication — before the first
        sync dispatches timeline events — so @-mention matching works from the
        very first event (AGENTTEAMS_WORKER_MATRIX_USER_ID env is not set on
        controller-managed bridge pods).
        """
        self.mention_filter.user_id = user_id

    def _apply_env_overrides(self) -> None:
        """Deployment-level runtime routing overrides (pod env).

        The baked bridge config stays adapter-agnostic; a controller-managed
        bridge pod for runtime=opencode receives its endpoints through
        Worker CR spec.env (BRIDGE_RUNTIME_ADAPTER / _BASE_URL / _HELPER_URL).
        S3 bootstrap values, when present, still win (checked later in start()).
        """
        overrides = {
            "adapter": os.getenv("BRIDGE_RUNTIME_ADAPTER", ""),
            "base_url": os.getenv("BRIDGE_RUNTIME_BASE_URL", ""),
            "helper_url": os.getenv("BRIDGE_RUNTIME_HELPER_URL", ""),
        }
        for key, value in overrides.items():
            if value:
                setattr(self.config.runtime, key, value)

    def _build_state_store(self) -> Any:
        backend = self.config.store.backend
        if backend == "redis":
            url = os.getenv(self.config.store.redis_url_env, "")
            if url:
                return RedisStore(url)
            logger.warning("Redis backend selected but %s is missing; using memory", self.config.store.redis_url_env)
        if backend == "file":
            return FileStore()
        return MemoryStore()

    async def _refresh_matrix_token(self) -> str | None:
        controller_url = os.getenv("AGENTTEAMS_CONTROLLER_URL", "").rstrip("/")
        auth_token = os.getenv("AGENTTEAMS_AUTH_TOKEN", "")
        token_file = os.getenv("AGENTTEAMS_AUTH_TOKEN_FILE", "")
        if not auth_token and token_file:
            try:
                auth_token = Path(token_file).read_text(encoding="utf-8").strip()
            except OSError:
                return None
        if not controller_url or not auth_token:
            return None
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    f"{controller_url}/api/v1/credentials/matrix-token",
                    headers={"Authorization": f"Bearer {auth_token}"},
                )
                response.raise_for_status()
                token = response.json().get("access_token")
            return str(token) if token else None
        except Exception as exc:
            logger.warning("Matrix token refresh failed: %s", exc)
            return None

    async def start_background(self) -> None:
        if self.matrix_gateway is None:
            self.runtime_healthy = True
            self.ready = False
            self.phase = "bootstrap"
            return
        self.matrix_task = asyncio.create_task(self.matrix_gateway.start())
        while not self.matrix_gateway.connected and not self.matrix_task.done():
            await asyncio.sleep(0.05)
        self.matrix_connected = self.matrix_gateway.connected
        if self.matrix_gateway.user_id:
            self.mention_filter.user_id = self.matrix_gateway.user_id
            self.mention_filter.role_resolver.self_user_id = self.matrix_gateway.user_id
        self.runtime_healthy = self.matrix_gateway.connected
        session_required = self.config.runtime.adapter == "cimicode"
        self.ready = self.matrix_connected and (bool(self.config.runtime.session_id) or not session_required)
        self.phase = "listening" if self.ready else "bootstrap"

    def stop(self) -> None:
        self.phase = "stopping"
        self.ready = False
        print("Bridge shutdown requested")

    async def shutdown(self) -> None:
        if self.matrix_gateway is not None:
            await self.matrix_gateway.stop()
        if self.matrix_task is not None:
            self.matrix_task.cancel()
            await asyncio.gather(self.matrix_task, return_exceptions=True)
        closer = getattr(self.runtime_client, "close", None)
        if closer is not None:
            await closer()

    async def handle_matrix_message(
        self,
        room_id: str,
        sender: str,
        event_id: str,
        content: dict[str, Any],
    ) -> None:
        body = str(content.get("body", ""))
        decision = self.mention_filter.evaluate(body, sender, content=content)
        logger.info(
            "matrix message room=%s sender=%s event=%s accepted=%s role=%s reason=%s mentions=%s body=%r",
            room_id,
            sender,
            event_id,
            decision.accepted,
            decision.role,
            decision.reason,
            decision.mentions,
            body[:80],
        )
        history = self.history_stores.setdefault(
            room_id,
            HistoryStore(capacity=self.config.history.max_entries),
        )
        if not decision.accepted:
            if decision.reason == "not_mentioned" and decision.role in self.mention_filter.allowed_roles:
                history.append(sender, body, event_id=event_id)
            return
        if self.runtime_client is None or self.matrix_gateway is None:
            return
        if self.config.runtime.adapter == "cimicode" and (
            not self.config.runtime.session_id or not self.config.runtime.sandbox_id
        ):
            logger.error("Gateway session binding is missing from S3 configuration")
            return

        user_message = history.build_context(f"{sender}: {body}")
        await self.matrix_gateway.start_typing(room_id)
        try:
            if self.config.runtime.adapter == "opencode":
                # v2.4: render agent.md from runtime.yaml + SOUL/PROFILE via
                # the generator shipped in the bridge image (fail-loud — a
                # failed render refuses the turn instead of sending a
                # half-configured system prompt to the sandbox).
                if self.worker_files is None or not self.worker_files.runtime_yaml:
                    logger.error(
                        "opencode adapter requires runtime/runtime.yaml in the "
                        "worker bootstrap (agents/<name>/runtime/runtime.yaml); refusing turn"
                    )
                    return
                try:
                    agent_md = build_agent_md_via_generator(
                        runtime_yaml=self.worker_files.runtime_yaml,
                        soul_md=self.worker_files.soul_md,
                        profile_md=self.worker_files.profile_md,
                    )
                except GenerateAgentMdError as exc:
                    logger.error("agent.md generation failed: %s", exc)
                    return
            else:
                agent_md = build_agent_md(
                    agents_md=self.worker_files.agents_md if self.worker_files else "",
                    soul_md=self.worker_files.soul_md if self.worker_files else "",
                    role=os.getenv("COORDINATION_ROLE", "worker"),
                    leader=os.getenv("COORDINATION_LEADER", ""),
                    team=os.getenv("COORDINATION_TEAM", ""),
                    room=os.getenv("COORDINATION_ROOM", room_id),
                    admin=os.getenv("COORDINATION_ADMIN", ""),
                    workers=os.getenv("COORDINATION_WORKERS", ""),
                )
            events = await self.runtime_client.chat(
                session_id=self.config.runtime.session_id,
                sandbox_id=self.config.runtime.sandbox_id,
                turn_id=event_id,
                agent_md=agent_md,
                history=[],
                user_message=user_message,
            )
            response_text = ""
            for event in events:
                if event.kind.value == "text_delta":
                    response_text += event.text
                elif event.kind.value == "turn_completed":
                    response_text = event.text or response_text
                elif event.kind.value in {"runtime_error", "turn_interrupted"}:
                    logger.error("Gateway turn failed: %s", event.data or event.text)
                    return
            if response_text.strip() and response_text.strip() != "NO_REPLY":
                await self.matrix_gateway.send_text(room_id, response_text)
            history.clear()
        except Exception:
            logger.exception("Matrix message handling failed event_id=%s", event_id)
        finally:
            await self.matrix_gateway.stop_typing(room_id)

    def status_payload(self) -> dict[str, Any]:
        return {
            "worker": "cimicode-bridge",
            "phase": self.phase,
            "runtime": self.config.runtime.adapter if self.config else "unknown",
            "matrix_connected": self.matrix_connected,
            "runtime_healthy": self.runtime_healthy,
            "ready": self.ready,
        }


def create_app() -> FastAPI:
    bridge = BridgeApp()
    bridge.start()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await bridge.start_background()
        try:
            yield
        finally:
            bridge.stop()
            await bridge.shutdown()

    app = FastAPI(title="cimicode-bridge", version="0.1.0", lifespan=lifespan)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> dict[str, bool]:
        return {"ready": bridge.ready}

    @app.get("/status")
    def status() -> dict[str, Any]:
        return bridge.status_payload()

    @app.post("/api/v1/bridge/handle-message")
    def handle_message(payload: dict[str, Any]) -> dict[str, Any]:
        body = str(payload.get("body", ""))
        sender = str(payload.get("sender", ""))
        event_id = str(payload.get("event_id", "evt-unknown"))
        room_id = str(payload.get("room_id", "unknown-room"))
        content = payload.get("content")

        decision = bridge.mention_filter.evaluate(body, sender, content=content)
        if not decision.accepted:
            if decision.reason == "not_mentioned" and decision.role in bridge.mention_filter.allowed_roles:
                history = bridge.history_stores.setdefault(
                    room_id,
                    HistoryStore(capacity=bridge.config.history.max_entries),
                )
                history.append(sender or "unknown", body, event_id=event_id)
            bridge.phase = "idle"
            return {
                "accepted": False,
                "forwarded": False,
                "reason": decision.reason,
                "session_id": None,
                "event_id": event_id,
                "room_id": room_id,
                "mentions": decision.mentions,
                "role": decision.role,
            }

        session_id = bridge.config.runtime.session_id or "configured-session"
        history = bridge.history_stores.setdefault(
            room_id,
            HistoryStore(capacity=bridge.config.history.max_entries),
        )
        user_message = history.build_context(f"{sender}: {body}")
        bridge.session_manager.start_session(session_id)
        bridge.session_manager.add_turn(session_id, event_id, body)
        history.clear()
        bridge.phase = "message_forwarded"
        bridge.matrix_connected = True
        bridge.runtime_healthy = True
        bridge.ready = True

        return {
            "accepted": True,
            "forwarded": True,
            "session_id": session_id,
            "event_id": event_id,
            "room_id": room_id,
            "mentions": decision.mentions,
            "sender": sender,
            "role": decision.role,
            "user_message": user_message,
            "sandbox_id": bridge.config.runtime.sandbox_id,
        }

    return app
