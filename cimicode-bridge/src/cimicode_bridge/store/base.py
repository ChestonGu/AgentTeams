"""StateStore SPI：异步 KV 接口（since 等运行状态的后端契约）。"""
from __future__ import annotations

from typing import Protocol


class StateStore(Protocol):
    """结构化状态存储接口：memory/file/redis 三后端共同实现。"""

    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None: ...
    async def delete(self, key: str) -> None: ...