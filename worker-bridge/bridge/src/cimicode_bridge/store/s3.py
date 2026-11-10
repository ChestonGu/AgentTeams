"""S3 StateStore：since 等运行状态持久化到 MinIO（agents/<w>/state/）。

第 4 个 StateStore 后端（memory/file/redis 之外）：复用 S3Bootstrap 的
客户端与 agents/<WORKER_NAME>/ 命名空间——pod 重启后续传 sync 游标，
增量拉取、不重放已处理消息。

scope 信封防毒：存值附带部署指纹（homeserver|worker），读取时不匹配
视为无——Synapse 重建 / 换环境后的旧 token 自动回落 full_state 自愈，
而不是毒化 sync 循环（毒 token 会让 sync 永久报错，bridge 变聋）。

已知限制（接受）：持久化发生在"批次处理完"之后——极端 crash-mid-batch
会重放那一批（at-most-one-batch 重复），不为它上 event_id 去重。
minio SDK 为同步调用：1 对象 GET/PUT 毫秒级，与 bootstrap 每 turn 拉
S3 的既有模式一致，不做 to_thread 异步化。
"""
from __future__ import annotations

import json
import logging

from cimicode_bridge.bootstrap import S3Bootstrap

logger = logging.getLogger(__name__)

_STATE_DIR = "state"


class S3StateStore:
    """MinIO KV（agents/<w>/state/<key>，scope 信封校验，值未变跳过写）。"""

    def __init__(self, bootstrap: S3Bootstrap, *, scope: str) -> None:
        self._bootstrap = bootstrap
        self._scope = scope
        self._last_written: dict[str, str] = {}

    def _key(self, key: str) -> str:
        return f"{_STATE_DIR}/{key}"

    async def get(self, key: str) -> str | None:
        raw = self._bootstrap.read_text(self._key(key))
        if raw is None:
            return None
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("s3 state object unreadable (key=%s); treating as absent", key)
            return None
        if not isinstance(envelope, dict) or envelope.get("scope") != self._scope:
            # 部署指纹不匹配：旧环境/异环境的 token 视为无 → 调用方回落
            # full_state 自愈
            logger.warning(
                "s3 state scope mismatch (key=%s); treating as absent — full_state fallback", key
            )
            return None
        value = envelope.get("value")
        return str(value) if value is not None else None

    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        if self._last_written.get(key) == value:
            return  # 值未变跳过写（next_batch 高频前进，省一次 PUT）
        envelope = json.dumps({"scope": self._scope, "value": value}, ensure_ascii=False)
        written = self._bootstrap.publish(self._key(key), envelope)
        if written is not None:
            self._last_written[key] = value

    async def delete(self, key: str) -> None:
        # S3Bootstrap 无删除原语——写 value=None 的信封等价删除（get 返回 None）
        self._last_written.pop(key, None)
        self._bootstrap.publish(self._key(key), json.dumps({"scope": self._scope, "value": None}))
