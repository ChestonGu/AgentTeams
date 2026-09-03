from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

from minio import Minio

logger = logging.getLogger(__name__)


@dataclass
class WorkerBootstrapConfig:
    openclaw: dict[str, Any]
    agents_md: str = ""
    soul_md: str = ""

    @property
    def matrix_config(self) -> dict[str, Any]:
        channels = self.openclaw.get("channels", {})
        return channels.get("matrix", {}) if isinstance(channels, dict) else {}

    @property
    def matrix_access_token(self) -> str:
        matrix = self.matrix_config
        return str(matrix.get("accessToken") or matrix.get("access_token") or "")

    @property
    def bridge_runtime_config(self) -> dict[str, Any]:
        bridge = self.openclaw.get("bridge", {})
        if not isinstance(bridge, dict):
            return {}
        runtime = bridge.get("runtime", {})
        return runtime if isinstance(runtime, dict) else {}

    @property
    def gateway_session_id(self) -> str:
        return str(
            self.bridge_runtime_config.get("sessionId")
            or self.bridge_runtime_config.get("session_id")
            or ""
        )

    @property
    def gateway_sandbox_id(self) -> str:
        return str(
            self.bridge_runtime_config.get("sandboxId")
            or self.bridge_runtime_config.get("sandbox_id")
            or ""
        )


class S3Bootstrap:
    def __init__(self, *, client: Minio, bucket: str, prefix: str = "") -> None:
        self.client = client
        self.bucket = bucket
        self.prefix = prefix.strip("/")

    @classmethod
    def from_environment(cls) -> "S3Bootstrap | None":
        endpoint = os.getenv("AGENTTEAMS_FS_ENDPOINT", "")
        access_key = os.getenv("AGENTTEAMS_FS_ACCESS_KEY", "")
        secret_key = os.getenv("AGENTTEAMS_FS_SECRET_KEY", "")
        bucket = os.getenv("AGENTTEAMS_FS_BUCKET", "")
        if not endpoint or not access_key or not secret_key or not bucket:
            return None

        endpoint = endpoint.removeprefix("http://").removeprefix("https://")
        secure = os.getenv("AGENTTEAMS_FS_SECURE", "").lower() in {"1", "true", "yes"}
        return cls(
            client=Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure),
            bucket=bucket,
            prefix=os.getenv("AGENTTEAMS_STORAGE_PREFIX", ""),
        )

    def _key(self, name: str) -> str:
        worker_name = os.getenv("AGENTTEAMS_WORKER_NAME", "")
        parts = [self.prefix, "agents", worker_name, name]
        return "/".join(part.strip("/") for part in parts if part.strip("/"))

    def read_text(self, name: str) -> str | None:
        try:
            response = self.client.get_object(self.bucket, self._key(name))
            try:
                return response.read().decode("utf-8")
            finally:
                response.close()
                response.release_conn()
        except Exception as exc:
            logger.warning("failed to read bootstrap object %s: %s", name, exc)
            return None

    def load(self, *, retries: int = 6, retry_interval_seconds: float = 5) -> WorkerBootstrapConfig | None:
        openclaw_text = None
        for attempt in range(retries):
            openclaw_text = self.read_text("openclaw.json")
            if openclaw_text:
                break
            if attempt + 1 < retries:
                time.sleep(retry_interval_seconds)
        if not openclaw_text:
            return None
        try:
            openclaw = json.loads(openclaw_text)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid S3 openclaw.json") from exc
        return WorkerBootstrapConfig(
            openclaw=openclaw,
            agents_md=self.read_text("AGENTS.md") or "",
            soul_md=self.read_text("SOUL.md") or "",
        )