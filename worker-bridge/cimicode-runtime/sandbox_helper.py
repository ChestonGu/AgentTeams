#!/usr/bin/env python3
"""Sandbox helper service for the cimicode pod (:4097).

cimicode（换皮 opencode）pod 是单容器执行环境：全套工具/skill/CLI
（taskflow / agentteams-sync / mc）与 opencode serve 同容器共存。本服务
承担两个入口：
  * POST /agents-md  bridge 每 turn 推送的 AGENTS.md 原文（原子写入
    工作目录，opencode 以 cwd 发现之）；
  * POST /exec       opencode 内置 bash 工具的转发端点（同名 custom
    tool tools/bash.ts 把命令路由到这里执行——同容器回环，不再有独立
    sandbox pod）。

Endpoints:
  POST /exec       {"command": str, "timeout"?: int} -> {exitCode, stdout, stderr}
  POST /agents-md  body = raw markdown (text/plain)  -> 200
  GET  /healthz                                     -> 200

可选共享令牌：SANDBOX_HELPER_TOKEN（校验 X-Sandbox-Token 头）。
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.getenv("BRIDGE_SANDBOX_HELPER_PORT", "4097"))
MAX_BODY = 2 * 1024 * 1024
MAX_EXEC_TIMEOUT = 900  # hard cap, seconds
TOKEN = os.getenv("SANDBOX_HELPER_TOKEN") or ""


def workdir() -> Path:
    root = os.getenv("AGENTTEAMS_FS_ROOT") or os.getenv("OPENCODE_WORKDIR") or os.getcwd()
    return Path(root)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _reply(self, status: int, payload: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _authorized(self) -> bool:
        if not TOKEN:
            return True
        return self.headers.get("X-Sandbox-Token") == TOKEN

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            raise ValueError("invalid body length")
        return self.rfile.read(length)

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        if self.path == "/healthz":
            self._reply(200, b'{"status":"ok"}')
            return
        self._reply(404, b'{"error":"not found"}')

    def do_POST(self) -> None:  # noqa: N802 - http.server naming
        if not self._authorized():
            self._reply(401, b'{"error":"unauthorized"}')
            return
        if self.path == "/agents-md":
            self._handle_agents_md()
            return
        if self.path == "/exec":
            self._handle_exec()
            return
        self._reply(404, b'{"error":"not found"}')

    def _handle_agents_md(self) -> None:
        try:
            body = self._read_body().decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            self._reply(400, b'{"error":"invalid body"}')
            return
        path = workdir() / "AGENTS.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        # atomic write: tmp file in the same directory, then replace
        fd = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(path.parent), delete=False, suffix=".agents-md.tmp"
        )
        try:
            fd.write(body)
            fd.flush()
            os.fsync(fd.fileno())
        finally:
            fd.close()
        os.replace(fd.name, path)
        self._reply(200, b'{"written":true}')

    def _handle_exec(self) -> None:
        try:
            req = json.loads(self._read_body().decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self._reply(400, b'{"error":"invalid JSON body"}')
            return
        command = req.get("command")
        if not isinstance(command, str) or not command.strip():
            self._reply(400, b'{"error":"command must be a non-empty string"}')
            return
        try:
            timeout = min(int(req.get("timeout") or 600), MAX_EXEC_TIMEOUT)
        except (TypeError, ValueError):
            timeout = 600

        try:
            proc = subprocess.run(
                ["/bin/sh", "-c", command],
                cwd=str(workdir()),
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
            )
            result = {
                "exitCode": proc.returncode,
                "stdout": proc.stdout or "",
                "stderr": proc.stderr or "",
            }
        except subprocess.TimeoutExpired:
            result = {"exitCode": -1, "stdout": "", "stderr": f"command timed out after {timeout}s"}
        self._reply(200, json.dumps(result).encode("utf-8"))

    def log_message(self, fmt: str, *args) -> None:  # keep stdout terse
        print(f"[sandbox-helper] {self.address_string()} {fmt % args}", flush=True)


def main() -> None:
    wd = workdir()
    wd.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[sandbox-helper] listening on :{PORT}, workdir={wd}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
