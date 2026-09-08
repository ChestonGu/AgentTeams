from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import logging
import os
import httpx
from pathlib import Path
import time
from typing import Any

from contextlib import asynccontextmanager
from fastapi import FastAPI

from cimicode_bridge.bootstrap import (
    S3Bootstrap,
    WorkerBootstrapConfig,
    managed_runtime_type,
)
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
    s3_bootstrap: S3Bootstrap | None = field(default=None, init=False)
    matrix_access_token: str = field(default="", init=False, repr=False)
    session_manager: SessionManager = field(default_factory=SessionManager)
    history_stores: dict[str, HistoryStore] = field(default_factory=dict)
    mention_filter: MentionFilter = field(default_factory=MentionFilter)
    matrix_gateway: MatrixGateway | None = field(default=None, init=False)
    runtime_client: Any | None = field(default=None, init=False)
    matrix_task: asyncio.Task[None] | None = field(default=None, init=False)
    recovery_task: asyncio.Task[None] | None = field(default=None, init=False)
    state_store: Any | None = field(default=None, init=False)

    def start(self) -> None:
        # Root-level config: without this the cimicode_bridge.* loggers
        # inherit WARNING and every INFO line (sync established, message
        # decisions) disappears while uvicorn's own access log stays visible.
        logging.basicConfig(
            level=os.getenv("BRIDGE_LOG_LEVEL", "INFO"),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        config_path = Path(self.config_path)
        self.config = load_config(config_path)
        self._apply_env_overrides()
        bootstrap = S3Bootstrap.from_environment()
        if bootstrap is not None:
            self.s3_bootstrap = bootstrap
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
            # Authoritative runtime type from the managed runtime.yaml —
            # self-provisions the opencode adapter + service URLs without
            # waiting for the operator's BRIDGE_RUNTIME_* env.
            if self.worker_files.runtime_yaml \
                    and managed_runtime_type(self.worker_files.runtime_yaml) == "opencode" \
                    and self.config.runtime.adapter != "opencode":
                self.config.runtime.adapter = "opencode"
                logger.info("adapter self-provisioned as opencode from runtime.yaml at boot")
            self._derive_opencode_urls()
            self.runtime_client = build_runtime_adapter(self.config.runtime)
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
        self.matrix_gateway = self._build_matrix_gateway()
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
        turn_timeout = os.getenv("BRIDGE_RUNTIME_TURN_TIMEOUT", "")
        if turn_timeout.isdigit() and int(turn_timeout) > 0:
            self.config.runtime.turn_timeout_seconds = int(turn_timeout)
        for key, value in overrides.items():
            if value:
                setattr(self.config.runtime, key, value)
        self._explicit_base_url = bool(overrides["base_url"])
        self._explicit_helper_url = bool(overrides["helper_url"])
        self._derive_opencode_urls()

    # Set when BRIDGE_RUNTIME_BASE_URL/_HELPER_URL (or the controller's
    # runtimeEnv) provided the value explicitly — derived URLs must not
    # shadow an explicit override.
    _explicit_base_url: bool = False
    _explicit_helper_url: bool = False

    def _derive_opencode_urls(self) -> None:
        """Self-provision the opencode wiring when the operator env is late.

        The runtime/sandbox service names follow the operator's predictable
        convention (opencode-<worker>-svc / opencode-<worker>-sandbox-svc),
        so the bridge does not need to wait for BRIDGE_RUNTIME_BASE_URL /
        _HELPER_URL to be patched into the Worker CR: compute them from the
        worker name and let explicit env values win as overrides. This
        removes the operator-env arrival race from the bootstrap path.
        """
        worker = os.getenv("AGENTTEAMS_WORKER_NAME", "")
        if not worker or self.config.runtime.adapter != "opencode":
            return
        if not self._explicit_base_url:
            self.config.runtime.base_url = f"http://opencode-{worker}-svc:4096"
        if not self._explicit_helper_url:
            self.config.runtime.helper_url = f"http://opencode-{worker}-sandbox-svc:4097"

    def _build_matrix_gateway(self) -> MatrixGateway | None:
        """Build the Matrix gateway from the current config, or None when the
        wiring is incomplete.

        Only the cimicode adapter requires a pre-created gateway session id
        (openclaw.json bridge.runtime.sessionId); the opencode adapter owns
        its session lifecycle itself. Late-arriving wiring (Worker spec.env
        written after the pod was created) is picked up by
        _recover_late_runtime_wiring, which reuses this builder.
        """
        matrix_config = self.worker_files.matrix_config if self.worker_files else {}
        homeserver = str(matrix_config.get("homeserver") or self.config.matrix.homeserver_url)
        if homeserver.startswith("${"):
            homeserver = os.getenv("AGENTTEAMS_MATRIX_URL", "")
        session_required = self.config.runtime.adapter == "cimicode"
        if homeserver and self.matrix_access_token and (self.config.runtime.session_id or not session_required):
            return MatrixGateway(
                homeserver,
                self.matrix_access_token,
                sync_timeout_seconds=self.config.matrix.sync_timeout_seconds,
                on_message=self.handle_matrix_message,
                state_store=self.state_store,
                since_key=f"matrix:since:{os.getenv('AGENTTEAMS_WORKER_NAME', 'worker')}",
                refresh_token=self._refresh_matrix_token,
                on_authenticated=self._on_matrix_authenticated,
            )
        return None

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

    # Poll interval for the late runtime-wiring recovery loop.
    RECOVERY_POLL_SECONDS = 15.0

    async def _fetch_runtime_env(self, worker: str, controller_url: str) -> dict[str, str]:
        """Fetch this worker's runtime-wiring env subset from the controller.

        GET /api/v1/workers/{self} is self-scoped: the worker authorization
        model already allows a worker to read its own CR (ActionGet +
        requireSelf). Returns {} on any failure — the recovery loop simply
        retries on the next tick.
        """
        auth_token = os.getenv("AGENTTEAMS_AUTH_TOKEN", "")
        token_file = os.getenv("AGENTTEAMS_AUTH_TOKEN_FILE", "")
        if not auth_token and token_file:
            try:
                auth_token = Path(token_file).read_text(encoding="utf-8").strip()
            except OSError:
                return {}
        if not auth_token:
            logger.warning("runtime wiring poll skipped: no auth token available")
            return {}
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(
                    f"{controller_url}/api/v1/workers/{worker}",
                    headers={"Authorization": f"Bearer {auth_token}"},
                )
                response.raise_for_status()
                runtime_env = response.json().get("runtimeEnv") or {}
            return {str(k): str(v) for k, v in runtime_env.items()}
        except Exception as exc:
            logger.warning("runtime wiring poll failed: %s", exc)
            return {}

    async def _recover_late_runtime_wiring(self) -> None:
        """Poll the controller for this worker's runtime wiring until it lands.

        A bridge pod can be created before the operator has written the
        runtime wiring (BRIDGE_RUNTIME_ADAPTER / _BASE_URL / _HELPER_URL)
        into Worker spec.env — the controller does not roll the pod after
        that write, so the env the process started with stays incomplete:
        the adapter defaults to cimicode with no gateway sessionId and the
        bridge wedges in phase=bootstrap forever. Instead of recreating the
        pod, poll GET /api/v1/workers/{self} until runtimeEnv carries the
        adapter, then rebuild the runtime adapter and the Matrix gateway
        in-process (same priority rules as startup: S3 bootstrap config
        would already have been applied; controller runtimeEnv fills the
        env-shaped gap).
        """
        worker = os.getenv("AGENTTEAMS_WORKER_NAME", "")
        controller_url = os.getenv("AGENTTEAMS_CONTROLLER_URL", "").rstrip("/")
        if not worker or not controller_url:
            logger.warning(
                "late runtime wiring recovery unavailable: AGENTTEAMS_WORKER_NAME / AGENTTEAMS_CONTROLLER_URL not set"
            )
            return
        logger.info(
            "bridge in bootstrap without a gateway; polling controller for late runtime wiring every %.0fs",
            self.RECOVERY_POLL_SECONDS,
        )
        last_adapter = ""
        while True:
            # Bug fix (r2 it-w1): a bridge pod that starts before the
            # controller pushes agents/<w>/runtime/runtime.yaml wedges here
            # forever if we only chase env wiring — the bootstrap objects
            # (and with them the matrix credentials) never load and the
            # gateway is never built. Retry the S3 bootstrap every cycle;
            # when it finally lands, rebuild the gateway in-process.
            if (self.worker_files is None or not self.worker_files.runtime_yaml) \
                    and self.s3_bootstrap is not None:
                refetched = self.s3_bootstrap.load(retries=1, retry_interval_seconds=0)
                if refetched is not None and refetched.runtime_yaml:
                    logger.info("bootstrap objects recovered by self-heal poll; rebuilding gateway")
                    self.worker_files = refetched
                    if not self.matrix_access_token:
                        self.matrix_access_token = refetched.matrix_access_token
                    runtime = refetched.bridge_runtime_config
                    self.config.runtime.base_url = str(
                        runtime.get("baseUrl") or runtime.get("base_url")
                        or self.config.runtime.base_url)
                    self.config.runtime.helper_url = (
                        refetched.runtime_helper_url or self.config.runtime.helper_url)
                    self.config.runtime.session_id = refetched.gateway_session_id
                    self.config.runtime.sandbox_id = refetched.gateway_sandbox_id
                    # The managed runtime.yaml is the authoritative runtime
                    # type — it self-provisions the opencode adapter (and the
                    # predictable service URLs) without waiting for the
                    # operator's BRIDGE_RUNTIME_* env to land.
                    if managed_runtime_type(refetched.runtime_yaml) == "opencode" \
                            and self.config.runtime.adapter != "opencode":
                        self.config.runtime.adapter = "opencode"
                        logger.info("adapter self-provisioned as opencode from runtime.yaml")
                    self._derive_opencode_urls()
                    if self.config.runtime.adapter == "opencode" and self.runtime_client is not None:
                        closer = getattr(self.runtime_client, "close", None)
                        if closer is not None:
                            await closer()
                    self.runtime_client = build_runtime_adapter(self.config.runtime)
                    gateway = self._build_matrix_gateway()
                    if gateway is not None:
                        self.matrix_gateway = gateway
                        self.matrix_task = asyncio.create_task(gateway.start())
                        while not gateway.connected and not self.matrix_task.done():
                            await asyncio.sleep(0.05)
                        self.matrix_connected = gateway.connected
                        self.runtime_healthy = gateway.connected
                        if gateway.connected:
                            self.phase = "listening" if (
                                self.config.runtime.adapter != "cimicode"
                                or self.config.runtime.session_id
                            ) else "bootstrap"
                            self.ready = self.phase == "listening"
                            logger.info(
                                "gateway rebuilt from late bootstrap (phase=%s); matrix sync will replay unconsumed mentions",
                                self.phase,
                            )
                            if self.phase == "listening":
                                return
            runtime_env = await self._fetch_runtime_env(worker, controller_url)
            adapter = str(runtime_env.get("BRIDGE_RUNTIME_ADAPTER", ""))
            if adapter and adapter != last_adapter:
                last_adapter = adapter
                for env_key, attr, flag in (
                    ("BRIDGE_RUNTIME_BASE_URL", "base_url", "_explicit_base_url"),
                    ("BRIDGE_RUNTIME_HELPER_URL", "helper_url", "_explicit_helper_url"),
                ):
                    value = str(runtime_env.get(env_key, ""))
                    if value:
                        setattr(self.config.runtime, attr, value)
                        setattr(self, flag, True)
                logger.info(
                    "late runtime wiring recovered from controller: adapter=%s base_url=%s helper_url=%s",
                    adapter,
                    self.config.runtime.base_url,
                    self.config.runtime.helper_url,
                )
                closer = getattr(self.runtime_client, "close", None)
                if closer is not None:
                    await closer()
                self.config.runtime.adapter = adapter
                self.runtime_client = build_runtime_adapter(self.config.runtime)
                self.matrix_gateway = self._build_matrix_gateway()
                if self.matrix_gateway is not None:
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
                    if self.phase == "listening":
                        logger.info(
                            "bridge recovered to listening without a pod restart (adapter=%s)",
                            adapter,
                        )
                        return
                    logger.warning(
                        "recovered wiring did not reach listening (phase=%s); keep polling for changes",
                        self.phase,
                    )
            await asyncio.sleep(self.RECOVERY_POLL_SECONDS)

    async def start_background(self) -> None:
        if self.matrix_gateway is None:
            self.runtime_healthy = True
            self.ready = False
            self.phase = "bootstrap"
            # Late runtime wiring (pod created before Worker spec.env landed)
            # self-heals by polling the controller instead of requiring a
            # pod recreation.
            self.recovery_task = asyncio.create_task(self._recover_late_runtime_wiring())
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
        if self.recovery_task is not None:
            self.recovery_task.cancel()
            await asyncio.gather(self.recovery_task, return_exceptions=True)
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
        turn_started = time.monotonic()
        logger.info(
            "turn start event_id=%s adapter=%s user_message=%r",
            event_id,
            self.config.runtime.adapter,
            user_message[:300],
        )
        try:
            if self.config.runtime.adapter == "opencode":
                # v2.4: render agent.md from runtime.yaml + SOUL/PROFILE via
                # the generator shipped in the bridge image (fail-loud — a
                # failed render refuses the turn instead of sending a
                # half-configured system prompt to the sandbox).
                #
                # The controller enriches runtime.yaml after its first write
                # (member.matrixUserId lands once the matrix user registers,
                # team facts update on membership changes), so the boot-time
                # bootstrap cache must never shadow the source of truth:
                # pull fresh from S3 on EVERY turn and fall back to the
                # cache only when the pull itself fails.
                turn_files = self.worker_files
                if self.s3_bootstrap is not None:
                    fresh = self.s3_bootstrap.load(retries=1)
                    if fresh is not None and fresh.runtime_yaml:
                        turn_files = fresh
                        self.worker_files = fresh
                    else:
                        logger.warning(
                            "per-turn bootstrap pull failed; falling back to boot-time cache"
                        )
                if turn_files is None or not turn_files.runtime_yaml:
                    logger.error(
                        "opencode adapter requires runtime/runtime.yaml in the "
                        "worker bootstrap (agents/<name>/runtime/runtime.yaml); refusing turn"
                    )
                    return
                try:
                    agent_md = build_agent_md_via_generator(
                        runtime_yaml=turn_files.runtime_yaml,
                        soul_md=turn_files.soul_md,
                        profile_md=turn_files.profile_md,
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
            if self.s3_bootstrap is not None:
                key = self.s3_bootstrap.publish("agent-md/latest.md", agent_md)
                logger.info(
                    "agent.md generated bytes=%d sha256=%s wrote=%s",
                    len(agent_md.encode("utf-8")),
                    hashlib.sha256(agent_md.encode("utf-8")).hexdigest()[:16],
                    f"s3://{self.s3_bootstrap.bucket}/{key}" if key else "(publish failed)",
                )
            else:
                logger.info("agent.md generated bytes=%d (no S3 bootstrap; not published)", len(agent_md.encode("utf-8")))
            events = await self.runtime_client.chat(
                session_id=self.config.runtime.session_id,
                sandbox_id=self.config.runtime.sandbox_id,
                turn_id=event_id,
                agent_md=agent_md,
                history=[],
                user_message=user_message,
            )
            response_text = ""
            progress_texts: list[str] = []
            for event in events:
                if event.kind.value == "text_delta":
                    response_text += event.text
                elif event.kind.value == "turn_completed":
                    response_text = event.text or response_text
                    progress_texts = list((event.data or {}).get("progress_texts") or [])
                elif event.kind.value in {"runtime_error", "turn_interrupted"}:
                    logger.error("Gateway turn failed: %s", event.data or event.text)
                    # A silent failure leaves the room waiting forever (the
                    # delegating leader has no other failure signal). Tell the
                    # room what happened so the turn can be retried.
                    try:
                        reason = (event.text or "runtime error").strip()
                        await self.matrix_gateway.send_text(
                            room_id,
                            f"**turn failed**: {reason}",
                        )
                    except Exception:
                        logger.exception("failed to report turn failure to room %s", room_id)
                    return
            # Mid-turn narration (the agent's Progress updates between tool
            # calls) would otherwise be dropped with the final-reply-only
            # contract — forward each before the reply so multi-step work is
            # visible in the room as it happens.
            for seq, ptext in enumerate(progress_texts, 1):
                if ptext.strip() and ptext.strip() != "NO_REPLY":
                    await self.matrix_gateway.send_text(room_id, ptext)
                    logger.info(
                        "progress sent room=%s event_id=%s seq=%d/%d body=%r",
                        room_id,
                        event_id,
                        seq,
                        len(progress_texts),
                        ptext,
                    )
            if response_text.strip() and response_text.strip() != "NO_REPLY":
                await self.matrix_gateway.send_text(room_id, response_text)
                logger.info(
                    "reply sent room=%s event_id=%s bytes=%d body=%r",
                    room_id,
                    event_id,
                    len(response_text.encode("utf-8")),
                    response_text,
                )
            else:
                logger.info("no reply event_id=%s (empty or NO_REPLY marker)", event_id)
            history.clear()
            logger.info("turn completed event_id=%s elapsed=%.1fs", event_id, time.monotonic() - turn_started)
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
