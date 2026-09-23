"""fake-platform：stateless 模式的外部平台模拟件（105 回归测试用）。

模拟 cimicode stateless 平台的 SSE 网关契约（adapter-contract §3.1）：
- POST /v1/gateway/session/chat：记录完整请求体（stdout + /tmp/last-chat.json），
  回 message delta + done 两帧 SSE；
- GET /session：200（健康探测兼容）。

done 帧回显收到的绑定字段（eid/sessionId/sandboxId），使"请求体透传了
哪些 runtimeParameter"在群聊消息与 kubectl logs 里双可见。

部署：worker-bridge/test/fake-platform/deploy.yaml（ConfigMap 挂载本文件，
复用 agentteams/worker-bridge 镜像的 python3，零外网拉取）。
"""
from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LAST_CHAT_PATH = "/tmp/last-chat.json"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server 约定
        if self.path == "/session":
            self._send_json(200, {"ok": True})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - http.server 约定
        if self.path != "/v1/gateway/session/chat":
            self._send_json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        # 请求体落双份：kubectl logs 可查 + pod 内 exec cat 可查
        print(f"[fake-platform] chat request body: {json.dumps(body, ensure_ascii=False)}", flush=True)
        try:
            with open(LAST_CHAT_PATH, "w", encoding="utf-8") as fh:
                json.dump(body, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass
        # 回显绑定字段——eid 透传验证的直接证据
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

    def log_message(self, fmt: str, *args) -> None:  # 静默默认访问日志
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 4096), Handler)
    print(f"[fake-platform] listening on :4096 since {time.strftime('%FT%T')}", flush=True)
    server.serve_forever()
