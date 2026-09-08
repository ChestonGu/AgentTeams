"""应用编排：FastAPI 工厂 + BridgeApp 生命周期 + 消息主链路（过滤→三段式→turn→回发）。

职责划分：HTTP 端点在 api/routes，过滤决策在 matrix/filter，群聊视野 buffer 在
session.HistoryManager，gateway 调用在 runtime（cimicode SSE / opencode 轮询，
由 runtime/registry 工厂分派），Matrix 收发在 matrix/gateway，controller 交互
（401 刷新 / 晚到接线自愈）在 controller/client——本模块只做装配与串联。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Any, ClassVar

from contextlib import asynccontextmanager
from fastapi import FastAPI

from cimicode_bridge.api.routes import register_routes
from cimicode_bridge.bootstrap import S3Bootstrap, WorkerBootstrapConfig, managed_runtime_type
from cimicode_bridge.config import BridgeConfig, load_config
from cimicode_bridge.controller.client import fetch_worker_runtime_env, refresh_matrix_token
from cimicode_bridge.matrix.filter import MentionFilter, RoleResolver
from cimicode_bridge.matrix.gateway import MatrixGateway
from cimicode_bridge.prompt import GenerateAgentMdError, build_agent_md_via_generator
from cimicode_bridge.render import build_agent_md
from cimicode_bridge.runtime.registry import build_runtime_adapter
from cimicode_bridge.session import HistoryManager, SessionManager
from cimicode_bridge.store.file import FileStore
from cimicode_bridge.store.memory import MemoryStore
from cimicode_bridge.store.redis import RedisStore

logger = logging.getLogger(__name__)


@dataclass
class BridgeApp:
    """bridge 全局状态与消息处理中枢。

    生命周期：start()（同步装配）→ start_background()（异步起 Matrix 循环；
    接线不全时改起自愈轮询）→ handle_matrix_message()（事件驱动）→ shutdown()。
    """

    config_path: str = "config/bridge.example.yaml"  # 本地 YAML 配置路径（默认值兜底）
    debug: bool = False
    phase: str = "bootstrap"          # 当前阶段（bootstrap/listening/idle/stopping）
    matrix_connected: bool = False    # Matrix sync 是否已连接
    runtime_healthy: bool = False     # 运行时健康位
    ready: bool = False               # 探针就绪位（= Matrix 连接 + session 配置齐全）
    config: BridgeConfig | None = field(default=None, init=False)
    worker_files: WorkerBootstrapConfig | None = field(default=None, init=False)  # S3 拉取的配置
    s3_bootstrap: S3Bootstrap | None = field(default=None, init=False)           # S3 客户端（自愈轮询复用）
    matrix_access_token: str = field(default="", init=False, repr=False)          # 仅存内存
    session_manager: SessionManager = field(default_factory=SessionManager)       # turn 记录（调试）
    history_manager: HistoryManager = field(default_factory=HistoryManager)       # per-room 群聊视野 buffer
    mention_filter: MentionFilter = field(default_factory=MentionFilter)          # 收侧过滤器
    matrix_gateway: MatrixGateway | None = field(default=None, init=False)        # Matrix 传输层
    runtime_client: Any | None = field(default=None, init=False)                  # runtime adapter（cimicode/opencode）
    matrix_task: asyncio.Task[None] | None = field(default=None, init=False)      # sync 循环任务
    recovery_task: asyncio.Task[None] | None = field(default=None, init=False)    # 晚到接线自愈任务
    state_store: Any | None = field(default=None, init=False)                     # since 持久化后端
    # BRIDGE_RUNTIME_BASE_URL/_HELPER_URL（或 controller runtimeEnv）显式提供过
    # 值时置 True——按命名约定推导的 URL 不得遮蔽显式覆盖。
    _explicit_base_url: bool = field(default=False, init=False, repr=False)
    _explicit_helper_url: bool = field(default=False, init=False, repr=False)

    # 自愈轮询间隔（秒）
    RECOVERY_POLL_SECONDS: ClassVar = 15.0

    async def _fetch_runtime_env(self, worker: str, controller_url: str) -> dict[str, str]:
        """读本 worker 的 runtime 接线 env（controller/client 的薄委托，便于测试替换）。"""
        return await fetch_worker_runtime_env(worker, controller_url)

    def start(self) -> None:
        """同步装配：加载配置 → env 覆盖 → 拉 S3 → 装配过滤器/adapter/Matrix 网关。"""
        # 根级日志配置：不配的话 cimicode_bridge.* 只继承 WARNING，
        # INFO 级（sync 建立、消息决策）全被吞掉，而 uvicorn 自己的访问日志还在。
        logging.basicConfig(
            level=os.getenv("BRIDGE_LOG_LEVEL", "INFO"),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        config_path = Path(self.config_path)
        self.config = load_config(config_path)
        # 部署级 env 覆盖（pod env；Worker CR spec.env 注入）
        self._apply_env_overrides()
        # S3 bootstrap：拉 openclaw.json / runtime/runtime.yaml（重试 6×5s 等调谐写入）
        # + AGENTS.md + SOUL.md + PROFILE.md
        bootstrap = S3Bootstrap.from_environment()
        if bootstrap is not None:
            self.s3_bootstrap = bootstrap
            self.worker_files = bootstrap.load(retries=6, retry_interval_seconds=5)
            if self.worker_files is not None:
                self.matrix_access_token = self.worker_files.matrix_access_token
        # token 兜底：S3 没有则读 env（本地开发路径）
        if not self.matrix_access_token:
            self.matrix_access_token = os.getenv(self.config.matrix.token_env, "")
        # S3 bridge.runtime 段覆盖本地配置（baseUrl/templateId/sessionId/sandboxId/helperUrl）
        if self.worker_files is not None:
            runtime = self.worker_files.bridge_runtime_config
            self.config.runtime.base_url = str(runtime.get("baseUrl") or runtime.get("base_url") or self.config.runtime.base_url)
            self.config.runtime.template_id = str(runtime.get("templateId") or runtime.get("template_id") or self.config.runtime.template_id)
            self.config.runtime.helper_url = self.worker_files.runtime_helper_url or self.config.runtime.helper_url
            self.config.runtime.session_id = self.worker_files.gateway_session_id
            self.config.runtime.sandbox_id = self.worker_files.gateway_sandbox_id
            # managed runtime.yaml 是运行时类型的权威声明——不等 operator 的
            # BRIDGE_RUNTIME_* env，启动即自裁决 opencode adapter（含服务 URL 推导）。
            if self.worker_files.runtime_yaml \
                    and managed_runtime_type(self.worker_files.runtime_yaml) == "opencode" \
                    and self.config.runtime.adapter != "opencode":
                self.config.runtime.adapter = "opencode"
                logger.info("adapter self-provisioned as opencode from runtime.yaml at boot")
            self._derive_opencode_urls()
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
        # Runtime SPI 工厂：cimicode（SSE gateway）/ opencode（REST+轮询）。
        # adapter 选择同时决定下方的 session 绑定契约。
        self.runtime_client = build_runtime_adapter(self.config.runtime)
        self.state_store = self._build_state_store()
        self.matrix_gateway = self._build_matrix_gateway()
        self.runtime_healthy = False
        self.ready = False
        if self.debug:
            print(f"Loaded bridge config from {config_path}")
        print(f"Bridge started with runtime adapter: {self.config.runtime.adapter}")

    def _on_matrix_authenticated(self, user_id: str) -> None:
        """把 whoami 解析出的身份喂给 mention 过滤器。

        由 gateway 在认证完成后（首个 sync 派发 timeline 事件之前）回调——
        这样 @-mention 匹配从第一个事件起就生效（controller 管理的 bridge
        pod 上 AGENTTEAMS_WORKER_MATRIX_USER_ID env 并不设置）。
        """
        self.mention_filter.user_id = user_id

    def _apply_env_overrides(self) -> None:
        """部署级 runtime 路由覆盖（pod env）。

        固化进镜像的 bridge 配置保持 adapter 无关；runtime=opencode 的
        controller 托管 pod 经 Worker CR spec.env 收到端点
        （BRIDGE_RUNTIME_ADAPTER / _BASE_URL / _HELPER_URL）。S3 bootstrap
        值存在时仍然优先（在 start() 后段检查）。
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

    def _derive_opencode_urls(self) -> None:
        """operator env 晚到时自推导 opencode 接线。

        runtime/sandbox 服务名遵循 operator 的可预期约定
        （opencode-<worker>-svc / opencode-<worker>-sandbox-svc），因此 bridge
        无需等 BRIDGE_RUNTIME_BASE_URL / _HELPER_URL 被 patch 进 Worker CR：
        从 worker 名直接算出来，显式 env 值作为覆盖优先。这消除了启动路径上
        的 operator-env 竞争。
        """
        worker = os.getenv("AGENTTEAMS_WORKER_NAME", "")
        if not worker or self.config.runtime.adapter != "opencode":
            return
        if not self._explicit_base_url:
            self.config.runtime.base_url = f"http://opencode-{worker}-svc:4096"
        if not self._explicit_helper_url:
            self.config.runtime.helper_url = f"http://opencode-{worker}-sandbox-svc:4097"

    def _build_matrix_gateway(self) -> MatrixGateway | None:
        """按当前配置构建 Matrix 网关；接线不全时返回 None（交给自愈轮询）。

        只有 cimicode adapter 要求预创建的 gateway sessionId
        （openclaw.json bridge.runtime.sessionId）；opencode adapter 自管
        会话生命周期。晚到的接线（pod 创建后 Worker spec.env 才写入）由
        _recover_late_runtime_wiring 兜住，它复用本构建器。
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
                refresh_token=refresh_matrix_token,
                on_authenticated=self._on_matrix_authenticated,
            )
        return None

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

    async def _recover_late_runtime_wiring(self) -> None:
        """轮询 controller 直到本 worker 的 runtime 接线到位。

        bridge pod 可能在 operator 把 runtime 接线（BRIDGE_RUNTIME_ADAPTER /
        _BASE_URL / _HELPER_URL）写进 Worker spec.env 之前被创建——controller
        在那次写入后并不会滚动 pod，进程启动时的 env 因此残缺：adapter 默认
        cimicode 又没有 gateway sessionId，bridge 永远卡在 phase=bootstrap。
        与其重建 pod，不如轮询 GET /api/v1/workers/{self} 直到 runtimeEnv 携带
        adapter，然后**进程内**重建 runtime adapter 与 Matrix 网关（优先级与
        启动时一致：S3 bootstrap 配置已应用；controller runtimeEnv 补 env 缺口）。
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
            # 修复（r2 it-w1）：早于 controller 推送 agents/<w>/runtime/runtime.yaml
            # 启动的 bridge pod 若只追 env 接线会永远卡死——bootstrap 对象
            #（连带 matrix 凭证）始终不加载，网关也永远建不起来。每轮重试
            # S3 bootstrap；落地后在进程内重建网关。
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
                    # managed runtime.yaml 是运行时类型的权威声明——据此自裁决
                    # opencode adapter（并推导可预期的服务 URL）。
                    if managed_runtime_type(refetched.runtime_yaml) == "opencode" \
                            and self.config.runtime.adapter != "opencode":
                        self.config.runtime.adapter = "opencode"
                        logger.info("adapter self-provisioned as opencode from runtime.yaml")
                    self._derive_opencode_urls()
                    if self.runtime_client is not None:
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
        """异步启动：接线全则起 Matrix sync 循环；否则起自愈轮询等接线。"""
        if self.matrix_gateway is None:
            # 未配置 Matrix（接线不全）：保持不就绪，由自愈轮询兜底
            self.runtime_healthy = True
            self.ready = False
            self.phase = "bootstrap"
            self.recovery_task = asyncio.create_task(self._recover_late_runtime_wiring())
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
        # session 门禁 adapter 化：只有 cimicode 要求预创建的 sessionId
        session_required = self.config.runtime.adapter == "cimicode"
        self.ready = self.matrix_connected and (bool(self.config.runtime.session_id) or not session_required)
        self.phase = "listening" if self.ready else "bootstrap"

    def stop(self) -> None:
        """标记停机（探针翻 false）。"""
        self.phase = "stopping"
        self.ready = False
        print("Bridge shutdown requested")

    async def shutdown(self) -> None:
        """真正清理：取消自愈任务、关 Matrix 客户端、关 runtime adapter。"""
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
        """主链路：过滤 → 三段式组装 → adapter chat（SSE 或轮询）→ 回发 Matrix。

        typing 指示覆盖整个 turn（finally 保证成功/失败/NO_REPLY 均停止）。
        turn 失败时向房间发 "**turn failed**: ..."（静默失败会让房间永远
        等下去——委派的 leader 没有别的失败信号）。
        """
        body = str(content.get("body", ""))
        decision = self.mention_filter.evaluate(body, sender, content=content)
        # 全量决策日志（含丢弃）：原因/角色/mentions 可见
        logger.info(
            "matrix message room=%s sender=%s event=%s accepted=%s role=%s reason=%s mentions=%s body=%r",
            room_id, sender, event_id, decision.accepted, decision.role, decision.reason, decision.mentions, body[:80],
        )
        if not decision.accepted:
            # 白名单内的非 mention 消息进 room buffer（群聊视野）
            if decision.reason == "not_mentioned" and decision.role in self.mention_filter.allowed_roles:
                self.history_manager.record_ambient(room_id, sender, body, event_id=event_id)
            return
        if self.runtime_client is None or self.matrix_gateway is None:
            return
        # session 绑定缺失（仅 cimicode 需要；opencode 自管会话）→ 拒绝处理
        if self.config.runtime.adapter == "cimicode" and (
            not self.config.runtime.session_id or not self.config.runtime.sandbox_id
        ):
            logger.error("Gateway session binding is missing from S3 configuration")
            return

        # CoPaw 三段式群聊视野（history buffer + 当前消息）
        user_message = self.history_manager.build_context(room_id, sender, body)
        await self.matrix_gateway.start_typing(room_id)
        turn_started = time.monotonic()
        logger.info(
            "turn start event_id=%s adapter=%s user_message=%r",
            event_id, self.config.runtime.adapter, user_message[:300],
        )
        try:
            # ---- agent.md 组装（adapter 分支）----
            if self.config.runtime.adapter == "opencode":
                # v2.4：经 bridge 镜像内置的 generator 从 runtime.yaml +
                # SOUL/PROFILE 渲染 agent.md（fail-loud——渲染失败拒轮，
                # 绝不把半配置的 system prompt 发给 sandbox）。
                #
                # controller 会在首次写入后继续 enrich runtime.yaml
                #（member.matrixUserId 在 matrix 用户注册后落位，团队事实随
                # 成员变化更新），因此启动时的 bootstrap 缓存绝不能遮蔽真相：
                # 每 turn 从 S3 重拉，仅拉取失败时回退缓存。
                turn_files = self.worker_files
                if self.s3_bootstrap is not None:
                    fresh = self.s3_bootstrap.load(retries=1)
                    if fresh is not None and fresh.runtime_yaml:
                        turn_files = fresh
                        self.worker_files = fresh
                    else:
                        logger.warning("per-turn bootstrap pull failed; falling back to boot-time cache")
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
            # agent.md 回写 S3（观测通道，best-effort）
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

            # ---- 调 adapter（SSE 流聚合 / REST 轮询，统一返回 RuntimeEvent 列表）----
            events = await self.runtime_client.chat(
                session_id=self.config.runtime.session_id,
                sandbox_id=self.config.runtime.sandbox_id,
                turn_id=event_id,  # turnId = Matrix event_id（幂等键）
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
                    # done.content 是权威全文，覆盖增量拼接
                    response_text = event.text or response_text
                    progress_texts = list((event.data or {}).get("progress_texts") or [])
                elif event.kind.value in {"runtime_error", "turn_interrupted"}:
                    logger.error("Gateway turn failed: %s", event.data or event.text)
                    # 失败可见化：告诉房间发生了什么，turn 可被重试
                    try:
                        reason = (event.text or "runtime error").strip()
                        await self.matrix_gateway.send_text(
                            room_id,
                            f"**turn failed**: {reason}",
                        )
                    except Exception:
                        logger.exception("failed to report turn failure to room %s", room_id)
                    return
            # 中途叙述（工具调用间 agent 的 Progress 更新）若只按最终回复契约会被
            # 丢弃——逐条先发，多步工作在房间里实时可见。
            for seq, ptext in enumerate(progress_texts, 1):
                if ptext.strip() and ptext.strip() != "NO_REPLY":
                    await self.matrix_gateway.send_text(room_id, ptext)
                    logger.info(
                        "progress sent room=%s event_id=%s seq=%d/%d body=%r",
                        room_id, event_id, seq, len(progress_texts), ptext,
                    )
            # NO_REPLY：不发消息但照常清 buffer
            if response_text.strip() and response_text.strip() != "NO_REPLY":
                await self.matrix_gateway.send_text(room_id, response_text)
                logger.info(
                    "reply sent room=%s event_id=%s bytes=%d body=%r",
                    room_id, event_id, len(response_text.encode("utf-8")), response_text,
                )
            else:
                logger.info("no reply event_id=%s (empty or NO_REPLY marker)", event_id)
            self.history_manager.clear(room_id)
            logger.info("turn completed event_id=%s elapsed=%.1fs", event_id, time.monotonic() - turn_started)
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
