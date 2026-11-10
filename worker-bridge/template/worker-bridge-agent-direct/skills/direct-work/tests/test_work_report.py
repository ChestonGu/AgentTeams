#!/usr/bin/env python3
"""Tests for the direct-work skill's work_report CLI (direct collaboration).

Runs the CLI as a real subprocess with --sync none (no mc dependency) and a
local HTTP sink for the webhook path, mirroring the projectflow CLI tests.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "work_report.py"


def parse_status_events(stderr: str) -> list[dict]:
    events = []
    for line in stderr.splitlines():
        idx = line.find("STATUS_EVENT ")
        if idx >= 0:
            events.append(json.loads(line[idx + len("STATUS_EVENT "):]))
    return events


class _SinkHandler(BaseHTTPRequestHandler):
    received = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        _SinkHandler.received.append((self.path, dict(self.headers), body))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


class WorkReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "shared" / "config").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args, env_extra=None):
        env = dict(os.environ)
        env.pop("AGENTTEAMS_STATUS_WEBHOOK_URL", None)
        env.pop("AGENTTEAMS_STATUS_WEBHOOK_TOKEN", None)
        env["AGENTTEAMS_TEAM"] = "t-direct"
        env["AGENTTEAMS_MATRIX_USER_ID"] = "@dev:example.org"
        env.update(env_extra or {})
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args, "--sync", "none",
             "--root", str(self.root)],
            capture_output=True, text=True, env=env)

    def start_sink(self):
        _SinkHandler.received = []
        server = HTTPServer(("127.0.0.1", 0), _SinkHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def write_webhook_config(self, config: dict):
        (self.root / "shared" / "config" / "webhook.json").write_text(
            json.dumps(config))

    def test_started_event_fields(self):
        proc = self.run_cli("started", "--summary", "Collecting inputs",
                            "--requester", "@alice:example.org")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        events = parse_status_events(proc.stderr)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["event"], "direct_work_started")
        self.assertEqual(event["team"], "t-direct")
        self.assertEqual(event["actor"], "@dev:example.org")
        self.assertEqual(event["work"]["summary"], "Collecting inputs")
        self.assertEqual(event["work"]["requester"], "@alice:example.org")
        self.assertNotIn("deliverables", event["work"])
        self.assertNotIn("status", event["work"])
        self.assertIn("timestamp", event)

    def test_completed_event_with_deliverables_and_status(self):
        proc = self.run_cli(
            "completed", "--summary", "Report ready",
            "--deliverables", "shared/outputs/dev/report.md",
            "--requester", "@alice:example.org")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        event = parse_status_events(proc.stderr)[0]
        self.assertEqual(event["event"], "direct_work_completed")
        self.assertEqual(event["work"]["status"], "SUCCESS")
        self.assertEqual(event["work"]["deliverables"],
                         ["shared/outputs/dev/report.md"])

    def test_completed_blocked_status(self):
        proc = self.run_cli("completed", "--summary", "Blocked by missing API",
                            "--status", "BLOCKED")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        event = parse_status_events(proc.stderr)[0]
        self.assertEqual(event["work"]["status"], "BLOCKED")

    def test_empty_summary_rejected(self):
        proc = self.run_cli("started", "--summary", "   ")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(parse_status_events(proc.stderr), [])

    def test_webhook_from_shared_config_file_posts_envelope(self):
        server = self.start_sink()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/status"
            self.write_webhook_config({"AGENTTEAMS_STATUS_WEBHOOK_URL": url})
            proc = self.run_cli("completed", "--summary", "Done")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(len(_SinkHandler.received), 1)
            path, _, body = _SinkHandler.received[0]
            self.assertEqual(path, "/status")
            self.assertEqual(json.loads(body.decode("utf-8"))["event"],
                             "direct_work_completed")
            self.assertEqual(len(parse_status_events(proc.stderr)), 1)
        finally:
            server.shutdown()

    def test_webhook_env_wins_and_bearer_token(self):
        server = self.start_sink()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/env"
            self.write_webhook_config(
                {"AGENTTEAMS_STATUS_WEBHOOK_URL": "http://127.0.0.1:9/file"})
            proc = self.run_cli(
                "started", "--summary", "Go",
                env_extra={"AGENTTEAMS_STATUS_WEBHOOK_URL": url,
                           "AGENTTEAMS_STATUS_WEBHOOK_TOKEN": "secret-token"})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(len(_SinkHandler.received), 1)
            path, headers, _ = _SinkHandler.received[0]
            self.assertEqual(path, "/env")
            self.assertEqual(headers.get("Authorization"), "Bearer secret-token")
        finally:
            server.shutdown()

    def test_bad_config_file_degrades_to_log_only(self):
        (self.root / "shared" / "config" / "webhook.json").write_text("{not-json")
        proc = self.run_cli("started", "--summary", "Go")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(parse_status_events(proc.stderr)), 1)


if __name__ == "__main__":
    unittest.main()
