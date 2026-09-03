"""Tests for the unified JSONL logger (agentteams_log.py).

Contract (README / interface-contract §5.5): every tool logs to ONE file,
one JSON object per line, context-stamped (tool/worker/run_id); stderr
mirror on by default; file path from AGENTTEAMS_LOG_FILE /
AGENTTEAMS_LOG_DIR / $FS_ROOT/logs; logging must never break a command.
"""
import contextlib
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG))

import agentteams_log  # noqa: E402

TASKFLOW = PKG / "taskflow.py"


def read_lines(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


class ModuleTest(unittest.TestCase):
    def setUp(self):
        # reset the module's singleton state between tests
        agentteams_log._CONTEXT.clear()
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
        self.work = tempfile.mkdtemp(prefix="logtest-")

    def test_file_and_context_fields(self):
        path = os.path.join(self.work, "agentteams.log")
        os.environ["AGENTTEAMS_LOG_FILE"] = path
        os.environ["AGENTTEAMS_LOG_STDERR"] = "0"
        os.environ["AGENTTEAMS_WORKER_NAME"] = "log-worker"
        try:
            import time as _time
            log = agentteams_log.setup("taskflow", ["check", "t-1"])
            agentteams_log.log_event(log, "status_change", task="t-1",
                                     from_="assigned", to="in_progress")
            agentteams_log.log_cmd_end(log, 0, _time.monotonic() - 1.25,
                                       command="check")
        finally:
            os.environ.pop("AGENTTEAMS_LOG_FILE")
            os.environ.pop("AGENTTEAMS_LOG_STDERR")
            os.environ.pop("AGENTTEAMS_WORKER_NAME")
        entries = read_lines(path)
        self.assertEqual([e["event"] for e in entries],
                         ["cmd_start", "status_change", "cmd_end"])
        for entry in entries:
            self.assertEqual(entry["tool"], "taskflow")
            self.assertEqual(entry["worker"], "log-worker")
            self.assertTrue(entry["run_id"])
            self.assertIn("ts", entry)
        change = entries[1]
        self.assertEqual(change["from"], "assigned")  # from_ -> from
        self.assertEqual(change["to"], "in_progress")
        self.assertEqual(entries[2]["exit"], 0)
        self.assertGreaterEqual(entries[2]["duration_ms"], 1000)
        self.assertLess(entries[2]["duration_ms"], 60000)

    def test_path_resolution_order(self):
        env = {"AGENTTEAMS_LOG_FILE": "none"}
        self.assertIsNone(agentteams_log.resolve_log_file(env))
        env = {"AGENTTEAMS_LOG_FILE": "/tmp/x.log"}
        self.assertEqual(agentteams_log.resolve_log_file(env), "/tmp/x.log")
        env = {"AGENTTEAMS_LOG_DIR": "/tmp/dir"}
        self.assertEqual(agentteams_log.resolve_log_file(env),
                         os.path.join("/tmp/dir", "agentteams.log"))
        env = {"AGENTTEAMS_FS_ROOT": "/root/fs"}
        self.assertEqual(agentteams_log.resolve_log_file(env),
                         os.path.join("/root/fs", "logs", "agentteams.log"))
        self.assertEqual(
            agentteams_log.resolve_log_file({}),
            os.path.join("/root/agentteams-fs", "logs", "agentteams.log"))

    def test_clip_truncates_long_values(self):
        long_value = "x" * 500
        clipped = agentteams_log.clip(long_value)
        self.assertTrue(clipped.startswith("xxx"))
        self.assertTrue(clipped.endswith("(+300 chars)"))
        self.assertEqual(agentteams_log.clip_argv(["--spec", long_value])[1],
                         clipped)

    def test_unwritable_file_never_breaks_setup(self):
        os.environ["AGENTTEAMS_LOG_FILE"] = os.path.join(
            "\\\\?", "nonexistent-drive", "agentteams.log")
        os.environ["AGENTTEAMS_LOG_STDERR"] = "0"
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                log = agentteams_log.setup("taskflow", ["check"])  # must not raise
                agentteams_log.log_event(log, "status_change", task="t-1")
        finally:
            os.environ.pop("AGENTTEAMS_LOG_FILE")
            os.environ.pop("AGENTTEAMS_LOG_STDERR")
        # degraded-mode warning is stderr output too: suppressed when the
        # mirror is silenced (CLI stderr contracts stay byte-clean)
        self.assertEqual(err.getvalue(), "")


class CliIntegrationTest(unittest.TestCase):
    """A real taskflow run must leave its full trail in one JSONL file."""

    def test_ack_writes_status_change_trail(self):
        work = tempfile.mkdtemp(prefix="logcli-")
        root = os.path.join(work, "fs")
        log_path = os.path.join(work, "logs", "agentteams.log")
        task_dir = os.path.join(root, "shared", "tasks", "t-1")
        os.makedirs(task_dir)
        with open(os.path.join(task_dir, "meta.json"), "w",
                  encoding="utf-8", newline="\n") as fh:
            fh.write('{"task_id": "t-1", "project_id": "p-1", '
                     '"task_title": "T", "assigned_to": "bob", '
                     '"status": "assigned", "room_id": "!r:x.org"}\n')
        with open(os.path.join(task_dir, "spec.md"), "w",
                  encoding="utf-8", newline="\n") as fh:
            fh.write("# spec\ndo the thing\n")
        env = dict(os.environ,
                   AGENTTEAMS_FS_ROOT=root,
                   AGENTTEAMS_MATRIX_USER_ID="@bob:example.org",
                   AGENTTEAMS_WORKER_NAME="bob",
                   AGENTTEAMS_LOG_FILE=log_path,
                   AGENTTEAMS_LOG_STDERR="0")
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", str(TASKFLOW),
             "--sync", "none", "ack", "t-1"],
            capture_output=True, text=True, encoding="utf-8", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        entries = read_lines(log_path)
        events = [e["event"] for e in entries]
        self.assertEqual(events[0], "cmd_start")
        self.assertIn("status_change", events)
        self.assertEqual(events[-1], "cmd_end")
        change = next(e for e in entries if e["event"] == "status_change")
        self.assertEqual(change["task"], "t-1")
        self.assertEqual(change["from"], "assigned")
        self.assertEqual(change["to"], "in_progress")
        end = entries[-1]
        self.assertEqual(end["exit"], 0)
        self.assertEqual(end["command"], "ack")
        self.assertEqual(end["task"], "t-1")


if __name__ == "__main__":
    unittest.main()
