"""POST /api/v1/bridge/send 出站端点:沙箱 CLI 经 bridge 往指定房间发消息。

leader 委派通知的唯一出站通道;worker 形态不调用(模板禁止跨房),
端点对两种形态一视同仁——纯基础设施。
"""
import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from cimicode_bridge.api.routes import register_routes
from cimicode_bridge.app import BridgeApp


class FakeGateway:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, str, str | None]] = []

    async def send_text(self, room_id, text, *, content=None, tx_id=None):
        if self.fail:
            raise RuntimeError("nio down")
        self.calls.append((room_id, text, tx_id))
        return "$evt-1"


def _client(gateway) -> TestClient:
    bridge = BridgeApp()
    bridge.matrix_gateway = gateway
    api = FastAPI()
    register_routes(api, bridge)
    return TestClient(api)


def test_send_ok_returns_event_id():
    gw = FakeGateway()
    resp = _client(gw).post("/api/v1/bridge/send", json={
        "room_id": "!team:x", "text": "@w:dom assigned", "txn_id": "delegate-t1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"ok": True, "event_id": "$evt-1"}
    assert gw.calls == [("!team:x", "@w:dom assigned", "delegate-t1")]


def test_send_without_txn_id_passes_none():
    gw = FakeGateway()
    resp = _client(gw).post("/api/v1/bridge/send", json={
        "room_id": "!team:x", "text": "hello"})
    assert resp.json()["ok"] is True
    assert gw.calls[0][2] is None


def test_send_missing_room_id_or_text_rejected():
    gw = FakeGateway()
    resp = _client(gw).post("/api/v1/bridge/send", json={"text": "hi"})
    assert resp.json() == {"ok": False, "error": "room_id and text are required"}
    resp = _client(gw).post("/api/v1/bridge/send",
                            json={"room_id": "!team:x", "text": "  "})
    assert resp.json()["ok"] is False


def test_send_no_reply_text_rejected():
    gw = FakeGateway()
    resp = _client(gw).post("/api/v1/bridge/send", json={
        "room_id": "!team:x", "text": "NO_REPLY"})
    assert resp.json()["ok"] is False
    assert "NO_REPLY" in resp.json()["error"]


def test_send_gateway_not_ready():
    bridge = BridgeApp()  # matrix_gateway 默认 None
    api = FastAPI()
    register_routes(api, bridge)
    resp = TestClient(api).post("/api/v1/bridge/send", json={
        "room_id": "!team:x", "text": "hi"})
    assert resp.json() == {"ok": False, "error": "matrix gateway not ready"}


def test_send_gateway_failure_reported():
    resp = _client(FakeGateway(fail=True)).post("/api/v1/bridge/send", json={
        "room_id": "!team:x", "text": "hi"})
    assert resp.json()["ok"] is False
    assert "nio down" in resp.json()["error"]
