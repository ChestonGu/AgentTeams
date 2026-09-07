"""应用编排：FastAPI 工厂 + BridgeApp 生命周期 + Matrix→Gateway→Matrix 主链路。"""
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
from cimicode_bridge.render import build_agent_md
from cimicode_bridge.runtime.client import HttpSseRuntime
from cimicode_bridge.session import HistoryStore, SessionManager
from cimicode_bridge.store.file import FileStore
from cimicode_bridge.store.memory import MemoryStore
from cimicode_bridge.store.redis import RedisStore

logger = logging.getLogger(__name__)


@dataclass
class BridgeApp:
    """bridge 全局状态与消息处理中枢。

    生命周期：start()（同步装配）→ start_background()（异步起 Matrix 循环）
    → handle_matrix_message()（事件驱动）→ shutdown()。
    """

    config_path: str = "config/bridge.example.yaml"  # 本地 YAML 配置路径（默认值兜底）
    debug: bool = False
    phase: str = "bootstrap"          # 当前阶段（bootstrap/listening/idle/stopping）
    matrix_connected: bool = False    # Matrix sync 是否已连接
    runtime_healthy: bool = False     # 运行时健康位
    ready: bool = False               # 探针就绪位（= Matrix 连接 + session 配置齐全）
    config: BridgeConfig | None = field(default=None, init=False)
    worker_files: WorkerBootstrapConfig | None = field(default=None, init=False)  # S3 拉取的三件套
    matrix_access_token: str = field(default="", init=False, repr=False)          # 仅存内存
    session_manager: SessionManager = field(default_factory=SessionManager)       # turn 记录（调试）
    history_stores: dict[str, HistoryStore] = field(default_factory=dict)         # room_id → buffer
    mention_filter: MentionFilter = field(default_factory=MentionFilter)          # 收侧过滤器
    matrix_gateway: MatrixGateway | None = field(default=None, init=False)        # Matrix 传输层
    runtime_client: HttpSseRuntime | None = field(default=None, init=False)       # gateway 客户端
    matrix_task: asyncio.Task[None] | None = field(default=None, init=False)      # sync 循环任务
    state_store: Any | None = field(default=None, init=False)                     # since 持久化后端

    def start(self) -> None:
        """同步装配：加载配置 → 拉 S3 → 装配过滤器/runtime client/Matrix 网关。"""
        config_path = Path(self.config_path)
        self.config = load_config(config_path)
        # S3 bootstrap：拉 openclaw.json（重试 6×5s 等 调谐写入）+ AGENTS.md + SOUL.md
        bootstrap = S3Bootstrap.from_environment()
        if bootstrap is not None:
            self.worker_files = bootstrap.load(retries=6, retry_interval_seconds=5)
            if self.worker_files is not None:
                self.matrix_access_token = self.worker_files.matrix_access_token
        # token 兜底：S3 没有则读 env（本地开发路径）
        if not self.matrix_access_token:
            self.matrix_access_token = os.getenv(self.config.matrix.token_env, "")
        # S3 bridge.runtime 段覆盖本地配置（baseUrl/templateId/sessionId/sandboxId）
        if self.worker_files is not None:
            runtime = self.worker_files.bridge_runtime_config
            self.config.runtime.base_url = str(runtime.get("baseUrl") or runtime.get("base_url") or self.config.runtime.base_url)
            self.config.runtime.template_id = str(runtime.get("templateId") or runtime.get("template_id") or self.config.runtime.template_id)
            self.config.runtime.session_id = self.worker_files.gateway_session_id
            self.config.runtime.sandbox_id = self.worker_files.gateway_sandbox_id
        # 收侧过滤器：本地配置 + COORDINATION_* env 角色映射
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
        # gateway HTTP/SSE 客户端
        self.runtime_client = HttpSseRuntime(
            self.config.runtime.base_url,
            timeout_seconds=self.config.runtime.turn_timeout_seconds,
        )
        self.state_store = self._build_state_store()
        # Matrix homeserver：S3 优先，占位符 ${...} 则读 env；三者齐备才创建网关
        matrix_config = self.worker_files.matrix_config if self.worker_files else {}
        homeserver = str(matrix_config.get("homeserver") or self.config.matrix.homeserver_url)
        if homeserver.startswith("${"):
            homeserver = os.getenv("AGENTTEAMS_MATRIX_URL", "")
        if homeserver and self.matrix_access_token and self.config.runtime.session_id:
            self.matrix_gateway = MatrixGateway(
                homeserver,
                self.matrix_access_token,
                sync_timeout_seconds=self.config.matrix.sync_timeout_seconds,
                on_message=self.handle_matrix_message,
                state_store=self.state_store,
                since_key=f"matrix:since:{os.getenv('AGENTTEAMS_WORKER_NAME', 'worker')}",
                refresh_token=self._refresh_matrix_token,
            )
        self.runtime_healthy = False
        self.ready = False
        if self.debug:
            print(f"Loaded bridge config from {config_path}")
        print(f"Bridge started with runtime adapter: {self.config.runtime.adapter}")

    def _build_state_store(self) -> Any:
        """按配置选择 StateStore 后端：redis（无 URL 降级 memory）/ file / memory。"""
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
        """401 时调 controller 刷新 Matrix token（读 AUTH_TOKEN 或 token 文件）。"""
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
        """异步启动：拉起 Matrix sync 循环，等连接成功后翻转 ready。"""
        if self.matrix_gateway is None:
            # 未配置 Matrix（本地纯 HTTP 调试模式）：保持不就绪
            self.runtime_healthy = True
            self.ready = False
            self.phase = "bootstrap"
            return
        self.matrix_task = asyncio.create_task(self.matrix_gateway.start())
        # 轮询等待连接建立（或任务失败退出）
        while not self.matrix_gateway.connected and not self.matrix_task.done():
            await asyncio.sleep(0.05)
        self.matrix_connected = self.matrix_gateway.connected
        # whoami 拿到的真实 MXID 回填过滤器（修正启动前的 env 预判）
        if self.matrix_gateway.user_id:
            self.mention_filter.user_id = self.matrix_gateway.user_id
            self.mention_filter.role_resolver.self_user_id = self.matrix_gateway.user_id
        self.runtime_healthy = self.matrix_gateway.connected
        self.ready = self.matrix_connected and bool(self.config.runtime.session_id)
        self.phase = "listening" if self.ready else "bootstrap"

    def stop(self) -> None:
        """标记停机（探针翻 false）。"""
        self.phase = "stopping"
        self.ready = False
        print("Bridge shutdown requested")

    async def shutdown(self) -> None:
        """真正清理：关 Matrix 客户端、取消 sync 任务。"""
        if self.matrix_gateway is not None:
            await self.matrix_gateway.stop()
        if self.matrix_task is not None:
            self.matrix_task.cancel()
            await asyncio.gather(self.matrix_task, return_exceptions=True)

    async def handle_matrix_message(
        self,
        room_id: str,
        sender: str,
        event_id: str,
        content: dict[str, Any],
    ) -> None:
        """主链路：过滤 → 三段式组装 → gateway chat SSE → 回发 Matrix。

        typing 指示覆盖整个 turn（finally 保证成功/失败/NO_REPLY 均停止）。
        """
        body = str(content.get("body", ""))
        decision = self.mention_filter.evaluate(body, sender, content=content)
        # 丢弃必打日志（spec §7.6）：原因/角色/mentions 全量可见
        if not decision.accepted:
            logger.info(
                "message filtered event_id=%s sender=%s role=%s reason=%s mentions=%s body=%r",
                event_id, sender, decision.role, decision.reason, decision.mentions, body[:80],
            )
        history = self.history_stores.setdefault(
            room_id,
            HistoryStore(capacity=self.config.history.max_entries),
        )
        if not decision.accepted:
            # 白名单内的非 mention 消息进 room buffer（群聊视野）
            if decision.reason == "not_mentioned" and decision.role in self.mention_filter.allowed_roles:
                history.append(sender, body, event_id=event_id)
            return
        if self.runtime_client is None or self.matrix_gateway is None:
            return
        # session 绑定缺失（S3 未配置 sessionId/sandboxId）→ 拒绝处理
        if not self.config.runtime.session_id or not self.config.runtime.sandbox_id:
            logger.error("Gateway session binding is missing from S3 configuration")
            return

        # CoPaw 三段式群聊视野（history buffer + 当前消息）
        user_message = history.build_context(f"{sender}: {body}")
        await self.matrix_gateway.start_typing(room_id)
        try:
            events = await self.runtime_client.chat(
                session_id=self.config.runtime.session_id,
                sandbox_id=self.config.runtime.sandbox_id,
                turn_id=event_id,  # turnId = Matrix event_id（幂等键）
                agent_md=build_agent_md(
                    agents_md=self.worker_files.agents_md if self.worker_files else "",
                    soul_md=self.worker_files.soul_md if self.worker_files else "",
                    role=os.getenv("COORDINATION_ROLE", "worker"),
                    leader=os.getenv("COORDINATION_LEADER", ""),
                    team=os.getenv("COORDINATION_TEAM", ""),
                    room=os.getenv("COORDINATION_ROOM", room_id),
                    admin=os.getenv("COORDINATION_ADMIN", ""),
                    workers=os.getenv("COORDINATION_WORKERS", ""),
                ),
                history=[],
                user_message=user_message,
            )
            # 聚合 SSE 事件为完整回复文本
            response_text = ""
            for event in events:
                if event.kind.value == "text_delta":
                    response_text += event.text
                elif event.kind.value == "turn_completed":
                    response_text = event.text or response_text
                elif event.kind.value in {"runtime_error", "turn_interrupted"}:
                    logger.error("Gateway turn failed: %s", event.data or event.text)
                    return
            # NO_REPLY：不发消息但照常清 buffer
            if response_text.strip() and response_text.strip() != "NO_REPLY":
                await self.matrix_gateway.send_text(room_id, response_text)
            history.clear()
        except Exception:
            logger.exception("Matrix message handling failed event_id=%s", event_id)
        finally:
            await self.matrix_gateway.stop_typing(room_id)

    def status_payload(self) -> dict[str, Any]:
        """探针 /status 的响应体（leader 存活探测用）。"""
        return {
            "worker": "cimicode-bridge",
            "phase": self.phase,
            "runtime": self.config.runtime.adapter if self.config else "unknown",
            "matrix_connected": self.matrix_connected,
            "runtime_healthy": self.runtime_healthy,
            "ready": self.ready,
        }


def create_app() -> FastAPI:
    """FastAPI 应用工厂：装配 BridgeApp + lifespan + 全部 HTTP 端点。"""
    bridge = BridgeApp()
    bridge.start()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        """uvicorn 生命周期：启动时拉起后台任务，退出时清理。"""
        await bridge.start_background()
        try:
            yield
        finally:
            bridge.stop()
            await bridge.shutdown()

    app = FastAPI(title="cimicode-bridge", version="0.1.0", lifespan=lifespan)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """存活探针：进程在即 ok。"""
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> dict[str, bool]:
        """就绪探针：Matrix 连接 + session 配置齐备才 true。"""
        return {"ready": bridge.ready}

    @app.get("/status")
    def status() -> dict[str, Any]:
        """本地只读状态接口（leader 探活 worker 用，spec §7.5）。"""
        return bridge.status_payload()

    @app.post("/api/v1/bridge/handle-message")
    def handle_message(payload: dict[str, Any]) -> dict[str, Any]:
        """本地调试入口：模拟 Matrix 消息，复用过滤与三段式组装（不真正调 gateway）。"""
        body = str(payload.get("body", ""))
        sender = str(payload.get("sender", ""))
        event_id = str(payload.get("event_id", "evt-unknown"))
        room_id = str(payload.get("room_id", "unknown-room"))
        content = payload.get("content")

        decision = bridge.mention_filter.evaluate(body, sender, content=content)
        if not decision.accepted:
            # 被拒消息：白名单内的非 mention 进 buffer
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

        # 命中：组装三段式并记录 turn（HTTP 调试路径不真正调 gateway chat）
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
