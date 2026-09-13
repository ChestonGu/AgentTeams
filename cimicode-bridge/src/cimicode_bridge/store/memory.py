"""内存 StateStore：进程内 dict（默认后端，重启即失）。"""
from __future__ import annotations

from cimicode_bridge.store.base import StateStore


class MemoryStore(StateStore):
    """最简 KV 实现（TTL 参数忽略，仅为对齐接口）。"""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        self.values[key] = value

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)