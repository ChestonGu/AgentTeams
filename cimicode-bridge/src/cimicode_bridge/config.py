from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class MatrixConfig(BaseModel):
    homeserver_url: str = "${AGENTTEAMS_MATRIX_URL}"
    domain: str = "${AGENTTEAMS_MATRIX_DOMAIN}"
    token_env: str = "AGENTTEAMS_WORKER_MATRIX_TOKEN"
    since_persist: bool = True
    sync_timeout_seconds: int = 30
    e2ee: str = "off"


class FilterConfig(BaseModel):
    require_mention: bool = True
    allow_unknown: bool = False
    group_allow_from_worker: list[str] = ["leader", "admin", "human"]


class HistoryConfig(BaseModel):
    max_entries: int = 50
    record_interrupted: bool = False
    persist: bool = False
    rebuild_limit: int = 50


class RuntimeConfig(BaseModel):
    adapter: str = "cimicode"
    base_url: str = "http://cimicode-gateway"
    # opencode adapter: sandbox AGENTS.md helper endpoint (defaults to
    # base_url — the helper ships in the same sandbox image on :4097)
    helper_url: str = ""
    template_id: str = "default-template"
    session_id: str = ""
    sandbox_id: str = ""
    auth_type: str = "none"
    turn_timeout_seconds: int = 600
    poll_interval_seconds: float = 1.0
    submit_max_retries: int = 3
    queue_max_pending: int = 8


class EmitterConfig(BaseModel):
    no_reply_mode: str = "trim"
    streaming_mode: str = "complete"
    throttle_seconds: int = 3
    min_chunk_chars: int = 40
    markdown_html: bool = True
    thinking: str = "hide"
    tools: str = "compact"
    components: str = "placeholder"
    three_layer_mention: bool = True


class SystemPromptConfig(BaseModel):
    parts: list[str] = ["coordination", "agents_md", "soul_md"]


class StoreConfig(BaseModel):
    backend: str = "memory"
    redis_url_env: str = "BRIDGE_REDIS_URL"


class LifecycleConfig(BaseModel):
    probes_port: int = 8081
    report_ready_command: str = "agt worker report-ready --name ${AGENTTEAMS_WORKER_CR_NAME:-${AGENTTEAMS_WORKER_NAME}}"


class ShutdownConfig(BaseModel):
    cancel_turn: bool = True
    close_session: bool = True
    grace_seconds: int = 20


class BridgeConfig(BaseModel):
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
    p = Path(path)
    if not p.exists():
        return BridgeConfig()

    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return BridgeConfig.model_validate(raw)
