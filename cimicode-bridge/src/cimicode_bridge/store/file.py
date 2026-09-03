from __future__ import annotations

import json
from pathlib import Path


class FileStore:
    def __init__(self, path: str | Path = "bridge-state.json") -> None:
        self.path = Path(path)

    def _read(self) -> dict[str, str]:
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