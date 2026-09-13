"""Controller 交互客户端：Matrix token 刷新 / worker 自身 runtime 接线查询。

与 agi-agentteams-controller 的交互点：
  - 401 时换取新 Matrix token（POST /api/v1/credentials/matrix-token）
  - 自愈轮询读本 worker 的 runtimeEnv（GET /api/v1/workers/{self}，自作用域）
鉴权 token 来自 env `AGENTTEAMS_AUTH_TOKEN` 或 `AGENTTEAMS_AUTH_TOKEN_FILE` 文件。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def _load_auth_token() -> str:
    """读鉴权 token：AGENTTEAMS_AUTH_TOKEN 优先，退到 token 文件。"""
    auth_token = os.getenv("AGENTTEAMS_AUTH_TOKEN", "")
    token_file = os.getenv("AGENTTEAMS_AUTH_TOKEN_FILE", "")
    if not auth_token and token_file:
        try:
            auth_token = Path(token_file).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return auth_token


async def refresh_matrix_token() -> str | None:
    """调 controller 刷新 Matrix token，成功返回新 access_token，失败返回 None。

    对应接口：POST {AGENTTEAMS_CONTROLLER_URL}/api/v1/credentials/matrix-token
    """
    controller_url = os.getenv("AGENTTEAMS_CONTROLLER_URL", "").rstrip("/")
    auth_token = _load_auth_token()
    if not controller_url or not auth_token:
        return None
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{controller_url}/api/v1/credentials/matrix-token",
                headers={"Authorization": f"Bearer {auth_token}"},
            )
            response.raise_for_status()
            token = response.json().get("access_token")
        return str(token) if token else None
    except Exception as exc:
        logger.warning("Matrix token refresh failed: %s", exc)
        return None


async def fetch_worker_runtime_env(worker: str, controller_url: str) -> dict[str, str]:
    """读本 worker 的 runtime 接线 env 子集（自愈轮询用）。

    GET /api/v1/workers/{self} 是自作用域的：worker 授权模型本就允许 worker
    读自己的 CR（ActionGet + requireSelf）。任何失败返回 {}——自愈循环下一轮
    自然重试。
    """
    auth_token = _load_auth_token()
    if not auth_token:
        logger.warning("runtime wiring poll skipped: no auth token available")
        return {}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{controller_url}/api/v1/workers/{worker}",
                headers={"Authorization": f"Bearer {auth_token}"},
            )
            response.raise_for_status()
            runtime_env = response.json().get("runtimeEnv") or {}
        return {str(k): str(v) for k, v in runtime_env.items()}
    except Exception as exc:
        logger.warning("runtime wiring poll failed: %s", exc)
        return {}
