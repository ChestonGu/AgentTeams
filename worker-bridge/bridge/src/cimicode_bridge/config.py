"""配置模型：Pydantic v2 分节定义 + YAML 加载（文件缺失回退全默认值）。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class MatrixConfig(BaseModel):
    """Matrix 连接配置。"""

    homeserver_url: str = "${AGENTTEAMS_MATRIX_URL}"   # 占位符，启动时解析 env
    domain: str = "${AGENTTEAMS_MATRIX_DOMAIN}"        # mention 域
    token_env: str = "AGENTTEAMS_WORKER_MATRIX_TOKEN"  # 本地开发 fallback 的 env 名
    since_persist: bool = True                         # since 是否持久化
    sync_timeout_seconds: int = 30                     # 长轮询超时
    e2ee: str = "off"                                  # 首版不支持加密


class FilterConfig(BaseModel):
    """收侧过滤配置。"""

    require_mention: bool = True                       # 群聊必须 @ 才触发
    allow_unknown: bool = False                        # 未知发送者默认拒绝
    group_allow_from_worker: list[str] = ["leader", "admin", "human"]  # 白名单角色


class HistoryConfig(BaseModel):
    """CoPaw 三段式 buffer 配置。"""

    max_entries: int = 50                              # 滑窗上限（对齐 CoPaw 默认）
    record_interrupted: bool = False                   # 中断轮不落 buffer
    persist: bool = False                              # StateStore 持久化开关（未接线）
    rebuild_limit: int = 50                            # 重启 timeline 回拉条数（未实现）


class RuntimeConfig(BaseModel):
    """gateway runtime 配置（baseUrl/sessionId 等由 S3 bridge 段覆盖）。

    adapter 与 base_url 故意保持空默认：合法来源只有 runtime.yaml 顶层
    bridge 段（controller 投影）与 BRIDGE_RUNTIME_* env（operator patch /
    部署层覆盖），两者皆缺时 bridge 进入未定态由自愈轮询等待——绝不
    静默回落到一个写死的 mock 地址。
    """

    adapter: str = ""                                  # cimicode-stateless / cimicode-pod（空=未定）
    base_url: str = ""                                 # stateless 取 bridge 段；pod 取 operator env
    template_id: str = ""                              # 仅来自 CR 投影，无假默认
    session_id: str = ""                               # S3 下发的 gateway session
    sandbox_id: str = ""                               # S3 下发的 sandbox
    eid: str = ""                                      # 用户业务标识（runtimeParameter 已知键；Gateway v2 submit 必传 Header）
    auth_type: str = "none"                            # gateway 当前不鉴权
    # 注：runtimeParameter 袋本体不在此存副本——参数面就是 runtime.yaml
    # （bootstrap 每 turn 重拉，_apply_bridge_section 每轮重抽已知键；
    # 追加键留袋，不上 submit 信封）。
    # 长 turn（整轮 agent 执行动辄超 10 分钟）的 bridge 侧等待上限。
    # 部署级可用 BRIDGE_RUNTIME_TURN_TIMEOUT 覆盖。
    turn_timeout_seconds: int = 3600                   # turn 超时
    submit_max_retries: int = 3                        # 提交重试（未实现）
    queue_max_pending: int = 8                         # 排队上限（未实现）


class EmitterConfig(BaseModel):
    """出站消息策略配置（部分字段当前未接线，为生产化预留）。"""

    no_reply_mode: str = "trim"                        # NO_REPLY 识别方式
    streaming_mode: str = "complete"                   # complete/chunked/throttled
    throttle_seconds: int = 3
    min_chunk_chars: int = 40
    markdown_html: bool = True                         # Markdown 渲染开关
    thinking: str = "hide"                             # thinking 展示策略
    tools: str = "compact"                             # 工具调用展示策略
    components: str = "placeholder"                    # 组件展示策略
    three_layer_mention: bool = True                   # 三层 mention 开关


class SystemPromptConfig(BaseModel):
    """agentMd 组装开关（哪些段落参与拼装）。"""

    parts: list[str] = ["coordination", "agents_md", "soul_md"]


class StoreConfig(BaseModel):
    """StateStore 后端配置。"""

    backend: str = "memory"                            # memory / file / redis
    redis_url_env: str = "BRIDGE_REDIS_URL"            # redis 连接串的 env 名


class LifecycleConfig(BaseModel):
    """生命周期配置。"""

    probes_port: int = 8081                            # 探针端口
    report_ready_command: str = "agt worker report-ready --name ${AGENTTEAMS_WORKER_CR_NAME:-${AGENTTEAMS_WORKER_NAME}}"  # 未执行


class ShutdownConfig(BaseModel):
    """优雅停机配置。"""

    cancel_turn: bool = True
    close_session: bool = True
    grace_seconds: int = 20


class BridgeConfig(BaseModel):
    """顶层配置聚合（各节均有默认值，可整体被 YAML 覆盖）。"""

    matrix: MatrixConfig = Field(default_factory=MatrixConfig)
    filter: FilterConfig = Field(default_factory=FilterConfig)
    history: HistoryConfig = Field(default_factory=HistoryConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    emitter: EmitterConfig = Field(default_factory=EmitterConfig)
    system_prompt: SystemPromptConfig = Field(default_factory=SystemPromptConfig)
    store: StoreConfig = Field(default_factory=StoreConfig)
    lifecycle: LifecycleConfig = Field(default_factory=LifecycleConfig)
    shutdown: ShutdownConfig = Field(default_factory=ShutdownConfig)


def load_config(path: str | Path) -> BridgeConfig:
    """从 YAML 加载配置；文件不存在返回全默认值（本地/测试可裸跑）。"""
    p = Path(path)
    if not p.exists():
        return BridgeConfig()

    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return BridgeConfig.model_validate(raw)
