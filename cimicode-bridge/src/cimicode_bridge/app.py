"""应用编排：FastAPI 工厂 + BridgeApp 生命周期 + 消息主链路（过滤→三段式→turn→回发）。

职责划分：HTTP 端点在 api/routes，过滤决策在 matrix/filter，群聊视野 buffer 在
session.HistoryManager，gateway 单轮调用在 runtime/turn，Matrix 收发在 matrix/gateway，
controller 交互（401 刷新）在 controller/client——本模块只做装配与串联。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
from typing import Any

from contextlib import asynccontextmanager
from fastapi import FastAPI

from cimicode_bridge.api.routes import register_routes
from cimicode_bridge.bootstrap import S3Bootstrap, WorkerBootstrapConfig
from cimicode_bridge.config import BridgeConfig, load_config
from cimicode_bridge.controller.client import refresh_matrix_token
from cimicode_bridge.matrix.filter import MentionFilter, RoleResolver
from cimicode_bridge.matrix.gateway import MatrixGateway
from cimicode_bridge.runtime.client import HttpSseRuntime
from cimicode_bridge.runtime.turn import TurnRunner
from cimicode_bridge.session import HistoryManager, SessionManager
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
    history_manager: HistoryManager = field(default_factory=HistoryManager)       # per-room 群聊视野 buffer
    mention_filter: MentionFilter = field(default_factory=MentionFilter)          # 收侧过滤器
    matrix_gateway: MatrixGateway | None = field(default=None, init=False)        # Matrix 传输层
    runtime_client: HttpSseRuntime | None = field(default=None, init=False)       # gateway 客户端
    turn_runner: TurnRunner | None = field(default=None, init=False)              # gateway 单轮执行器
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
        # 群聊视野 buffer 容量（config.history.max_entries）
        self.history_manager = HistoryManager(capacity=self.config.history.max_entries)
        # gateway HTTP/SSE 客户端
        self.runtime_client = HttpSseRuntime(
            self.config.runtime.base_url,
            timeout_seconds=self.config.runtime.turn_timeout_seconds,
        )
        # gateway 单轮执行器（agentMd 组装 + chat 调用 + SSE 事件聚合）
        self.turn_runner = TurnRunner(config=self.config.runtime)
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
                refresh_token=refresh_matrix_token,
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
        if not decision.accepted:
            # 白名单内的非 mention 消息进 room buffer（群聊视野）
            if decision.reason == "not_mentioned" and decision.role in self.mention_filter.allowed_roles:
                self.history_manager.record_ambient(room_id, sender, body, event_id=event_id)
            return
        if self.runtime_client is None or self.matrix_gateway is None or self.turn_runner is None:
            return
        # session 绑定缺失（S3 未配置 sessionId/sandboxId）→ 拒绝处理
        if not self.config.runtime.session_id or not self.config.runtime.sandbox_id:
            logger.error("Gateway session binding is missing from S3 configuration")
            return

        # CoPaw 三段式群聊视野（history buffer + 当前消息）
        user_message = self.history_manager.build_context(room_id, sender, body)
        await self.matrix_gateway.start_typing(room_id)
        try:
            result = await self.turn_runner.run_turn(
                self.runtime_client,
                worker_files=self.worker_files,
                room_id=room_id,
                event_id=event_id,
                user_message=user_message,
            )
            # turn 失败（runtime_error/turn_interrupted）：不清 buffer，保留群聊视野
            if result.failed:
                return
            response_text = result.text
            # NO_REPLY：不发消息但照常清 buffer
            if response_text.strip() and response_text.strip() != "NO_REPLY":
                await self.matrix_gateway.send_text(room_id, response_text)
            self.history_manager.clear(room_id)
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
    register_routes(app, bridge)
    return app
