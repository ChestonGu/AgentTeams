"""HTTP 端点注册：探针（/healthz /readyz /status）+ 本地调试入口。

app.py 的 create_app 只做装配，全部路由在此集中注册；
调试端点复用过滤与三段式组装，但不真正调 gateway chat。
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from cimicode_bridge.session import HistoryManager


def register_routes(app: FastAPI, bridge) -> None:
    """把全部 HTTP 端点注册到 app 上（bridge 为 BridgeApp 实例）。"""

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """存活探针：进程在即 ok。"""
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> dict[str, bool]:
        """就绪探针：Matrix 连接 + session 配置齐备才 true。"""
        return {"ready": bridge.ready}

    @app.get("/status")
    def status() -> dict[str, Any]:
        """本地只读状态接口（leader 探活 worker 用，spec §7.5）。"""
        return bridge.status_payload()

    @app.post("/api/v1/bridge/handle-message")
    def handle_message(payload: dict[str, Any]) -> dict[str, Any]:
        """本地调试入口：模拟 Matrix 消息，复用过滤与三段式组装（不真正调 gateway）。"""
        body = str(payload.get("body", ""))
        sender = str(payload.get("sender", ""))
        event_id = str(payload.get("event_id", "evt-unknown"))
        room_id = str(payload.get("room_id", "unknown-room"))
        content = payload.get("content")

        decision = bridge.mention_filter.evaluate(body, sender, content=content)
        history_manager: HistoryManager = bridge.history_manager
        if not decision.accepted:
            # 被拒消息：白名单内的非 mention 进 buffer
            if decision.reason == "not_mentioned" and decision.role in bridge.mention_filter.allowed_roles:
                history_manager.record_ambient(room_id, sender or "unknown", body, event_id=event_id)
            bridge.phase = "idle"
            return {
                "accepted": False,
                "forwarded": False,
                "reason": decision.reason,
                "session_id": None,
                "event_id": event_id,
                "room_id": room_id,
                "mentions": decision.mentions,
                "role": decision.role,
            }

        # 命中：组装三段式并记录 turn（HTTP 调试路径不真正调 gateway chat）
        session_id = bridge.config.runtime.session_id or "configured-session"
        user_message = history_manager.build_context(room_id, sender, body)
        bridge.session_manager.start_session(session_id)
        bridge.session_manager.add_turn(session_id, event_id, body)
        history_manager.clear(room_id)
        bridge.phase = "message_forwarded"
        bridge.matrix_connected = True
        bridge.runtime_healthy = True
        bridge.ready = True

        return {
            "accepted": True,
            "forwarded": True,
            "session_id": session_id,
            "event_id": event_id,
            "room_id": room_id,
            "mentions": decision.mentions,
            "sender": sender,
            "role": decision.role,
            "user_message": user_message,
            "sandbox_id": bridge.config.runtime.sandbox_id,
        }
