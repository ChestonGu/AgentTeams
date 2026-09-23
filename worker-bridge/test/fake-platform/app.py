"""fake-platform：stateless 模式的外部平台模拟件（105 回归测试用）。

模拟 cimicode Gateway v2 双接口契约（299dec86 起 bridge 侧适配）：
- POST /agi/gateway/v1/turn/submit——异步受理：
  Header 必带 eid + X-Idempotency-Key；Body 信封 {data:{sessionId,
  agentPrompt, userMessage}} 严格三字段。回执 Result<TurnVO>
  {data:{turnId, attemptId, queueStatus=QUEUED}}。请求全量落盘
  （stdout + /tmp/last-chat.json），eid 透传验证的直接证据在 Header。
- GET /agi/gateway/v1/session/{sid}/events——SSE 订阅（session 维度）：
  回放最近一次 submit 的回复——一条 delta + 一条终态
  invocation.yielded（content 回显收到的绑定字段），终态帧后关流
  （契约：终态帧后服务端关闭连接是正常结束）。
- GET /session：200（健康探测兼容）。
- POST /v1/gateway/session/chat：v1 旧接口保留（向后兼容，现版 bridge
  已不走）。

部署：worker-bridge/test/fake-platform/deploy.yaml（ConfigMap 挂载本文件，
复用 agentteams/worker-bridge 镜像的 python3，零外网拉取）。
"""
from __future__ import annotations

import json
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LAST_CHAT_PATH = "/tmp/last-chat.json"
# 最近一次 submit 的回复载荷（session 维度 events 回放用；单副本模拟件，
# 每次受理覆盖——bridge 每 turn 都是 submit→订阅→终态，时序恒成立）
_PENDING_TURN: dict[str, str] = {}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self, frames: list[dict]) -> None:
        """按 Gateway v2 SSE 契约回帧：data: {envelope}，终态帧后关流。"""
        payload = "".join(
            f"data: {json.dumps(f, ensure_ascii=False)}\n\n" for f in frames
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - http.server 约定
        if self.path == "/session":
            self._send_json(200, {"ok": True})
            return
        if self.path.startswith("/agi/gateway/v1/session/") and self.path.endswith("/events"):
            sid = self.path[len("/agi/gateway/v1/session/") : -len("/events")]
            turn = _PENDING_TURN or {"content": "[fake-platform] no pending turn"}
            frames = [
                # 增量帧：delta 契约（无 part_id，走 payload.delta/text）
                {"type": "session.next.text.delta", "data": {"delta": "[fake-platform] "}},
                # 终态帧：互斥恰一次，content 全文；帧后关流=正常结束
                {"type": "invocation.yielded", "data": {"content": turn["content"]}},
            ]
            print(f"[fake-platform] events replay sid={sid} frames={len(frames)}", flush=True)
            self._sse(frames)
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - http.server 约定
        if self.path == "/agi/gateway/v1/turn/submit":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            eid = self.headers.get("eid", "")
            idem = self.headers.get("X-Idempotency-Key", "")
            # 请求全量落双份：kubectl logs 可查 + exec cat 可查（eid 在 Header——
            # v2 参数面就是 runtime.yaml，信封严格三字段，绑定字段不上车）
            record = {
                "eid": eid,
                "idempotencyKey": idem,
                "sessionId": (body.get("data") or {}).get("sessionId", ""),
                "userMessage": (body.get("data") or {}).get("userMessage", ""),
                "agentPromptBytes": len((body.get("data") or {}).get("agentPrompt") or ""),
            }
            print(f"[fake-platform] v2 submit: {json.dumps(record, ensure_ascii=False)}", flush=True)
            try:
                with open(LAST_CHAT_PATH, "w", encoding="utf-8") as fh:
                    json.dump(record, fh, ensure_ascii=False, indent=2)
            except OSError:
                pass
            content = (
                "[fake-platform] 收到 turn（Gateway v2 submit），"
                f"绑定字段回显: {json.dumps(record, ensure_ascii=False)}"
            )
            _PENDING_TURN.clear()
            _PENDING_TURN.update({"content": content, "ts": time.strftime("%FT%T")})
            turn_id = f"fake-turn-{uuid.uuid4().hex[:12]}"
            self._send_json(200, {"data": {"turnId": turn_id, "attemptId": turn_id, "queueStatus": "QUEUED"}})
            return
        if self.path == "/v1/gateway/session/chat":  # v1 旧接口（兼容保留）
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            print(f"[fake-platform] v1 chat request body: {json.dumps(body, ensure_ascii=False)}", flush=True)
            echo = {k: body.get(k, "") for k in ("eid", "sessionId", "sandboxId", "templateId")}
            reply = "[fake-platform] 收到 turn，绑定字段回显: " + json.dumps(echo, ensure_ascii=False)
            frames = (
                f'data: {json.dumps({"event": "message", "delta": "[fake-platform] "}, ensure_ascii=False)}\n\n'
                f'data: {json.dumps({"event": "done", "content": reply}, ensure_ascii=False)}\n\n'
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(frames)))
            self.end_headers()
            self.wfile.write(frames)
            return
        self._send_json(404, {"error": "not found"})

    def log_message(self, fmt: str, *args) -> None:  # 静默默认访问日志
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 4096), Handler)
    print(f"[fake-platform] listening on :4096 (Gateway v2) since {time.strftime('%FT%T')}", flush=True)
    server.serve_forever()
