"""Controller 交互客户端：Matrix token 刷新等 platform 侧接口。

与 agi-agentteams-controller 的唯一交互点（当前）：401 时换取新 Matrix token。
鉴权 token 来自 env `AGENTTEAMS_AUTH_TOKEN` 或 `AGENTTEAMS_AUTH_TOKEN_FILE` 文件。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)


async def refresh_matrix_token() -> str | None:
    """调 controller 刷新 Matrix token，成功返回新 access_token，失败返回 None。

    对应接口：POST {AGENTTEAMS_CONTROLLER_URL}/api/v1/credentials/matrix-token
    """
    controller_url = os.getenv("AGENTTEAMS_CONTROLLER_URL", "").rstrip("/")
    auth_token = os.getenv("AGENTTEAMS_AUTH_TOKEN", "")
    token_file = os.getenv("AGENTTEAMS_AUTH_TOKEN_FILE", "")
    if not auth_token and token_file:
        try:
            auth_token = Path(token_file).read_text(encoding="utf-8").strip()
        except OSError:
            return None
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
