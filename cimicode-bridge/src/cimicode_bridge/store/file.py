"""文件 StateStore：单个 JSON 文件（调试用，重启保留）。"""
from __future__ import annotations

import json
from pathlib import Path


class FileStore:
    """以 JSON 文件持久化的 KV 存储（每次写全量落盘，TTL 忽略）。"""

    def __init__(self, path: str | Path = "bridge-state.json") -> None:
        self.path = Path(path)

    def _read(self) -> dict[str, str]:
        """读全量（文件不存在返回空 dict）。"""
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    async def get(self, key: str) -> str | None:
        return self._read().get(key)

    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        values = self._read()
        values[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(values), encoding="utf-8")

    async def delete(self, key: str) -> None:
        values = self._read()
        values.pop(key, None)
        self.path.write_text(json.dumps(values), encoding="utf-8")