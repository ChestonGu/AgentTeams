"""Unit tests for the taskflow CLI (T1 scope: commands + core guardrails).

T2 extends these with: full state-machine transition matrix, push-failure
rollback, and the mc sync backend contract.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import taskflow as tf_cli  # noqa: E402
from taskflow import main  # noqa: E402


def make_workspace(base: Path, status: str = "assigned", assigned_to: str = "worker-a") -> Path:
    """Build a minimal shared/ tree with one task, copaw-style files."""
    task_dir = base / "shared" / "tasks" / "t-1"
    task_dir.mkdir(parents=True)
    meta = {
        "task_id": "t-1",
        "project_id": "p-1",
        "task_title": "Write the module",
        "assigned_to": assigned_to,
        "room_id": "!room:example.org",
        "status": status,
        "depends_on": [],
        "assigned_at": "2026-09-01T00:00:00Z",
    }
    (task_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    (task_dir / "spec.md").write_text("# Spec\n\nDeliver the parser module.\n")
    return base


def run_cli(root: Path, *argv: str, actor: str = "@worker-a:example.org") -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(["--root", str(root), "--actor", actor, "--sync", "none", *argv])
    return code, out.getvalue(), err.getvalue()


class TaskflowCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_workspace(Path(self._tmp.name))
        self.meta_path = self.root / "shared" / "tasks" / "t-1" / "meta.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def read_meta(self) -> dict:
        return json.loads(self.meta_path.read_text())

    # -- check ------------------------------------------------------------

    def test_check_shows_summary(self) -> None:
        code, out, _ = run_cli(self.root, "check", "t-1")
        self.assertEqual(code, 0)
        self.assertIn("status: assigned", out)
        self.assertIn("title: Write the module", out)
        self.assertIn("result: <none>", out)

    def test_check_unknown_task_fails(self) -> None:
        code, _, err = run_cli(self.root, "check", "nope")
        self.assertEqual(code, 1)
        self.assertIn("file not found", err)

    # -- ack ----------------------------------------------------------------

    def test_ack_moves_assigned_to_in_progress_and_prints_spec(self) -> None:
        code, out, err = run_cli(self.root, "ack", "t-1")
        self.assertEqual(code, 0, err)
        self.assertIn("assigned -> in_progress", out)
        self.assertIn("Deliver the parser module.", out)  # spec content in response
        meta = self.read_meta()
        self.assertEqual(meta["status"], "in_progress")
        self.assertTrue(meta.get("acknowledged_at"))

    def test_ack_rejects_wrong_identity(self) -> None:
        code, _, err = run_cli(self.root, "ack", "t-1", actor="@worker-b:example.org")
        self.assertEqual(code, 1)
        self.assertIn("assigned to worker-a", err)
        self.assertEqual(self.read_meta()["status"], "assigned")  # untouched

    def test_ack_rejects_submitted_state(self) -> None:
        run_cli(self.root, "ack", "t-1")
        self.run_submit()
        code, _, err = run_cli(self.root, "ack", "t-1")
        self.assertEqual(code, 1)
        self.assertIn("cannot ack", err)

    def test_ack_idempotent_when_in_progress(self) -> None:
        run_cli(self.root, "ack", "t-1")
        first = self.read_meta()
        code, out, err = run_cli(self.root, "ack", "t-1")
        self.assertEqual(code, 0, err)
        self.assertIn("already in_progress", out)
        self.assertIn("Deliver the parser module.", out)  # spec still printed
        second = self.read_meta()
        self.assertEqual(first["acknowledged_at"], second["acknowledged_at"])

    # -- submit ---------------------------------------------------------------

    def run_submit(self) -> tuple[int, str, str]:
        return run_cli(
            self.root, "submit", "t-1",
            "--status", "SUCCESS",
            "--summary", "Module delivered with tests.",
            "--deliverables", "workspace/parser.py",
        )

    def test_submit_writes_protocol_result_and_marks_submitted(self) -> None:
        run_cli(self.root, "ack", "t-1")
        code, out, err = self.run_submit()
        self.assertEqual(code, 0, err)
        self.assertIn("in_progress -> submitted (SUCCESS)", out)

        result = (self.root / "shared" / "tasks" / "t-1" / "result.md").read_text()
        self.assertEqual(
            result,
            "STATUS: SUCCESS\n"
            "SUMMARY: Module delivered with tests.\n"
            "\n"
            "DELIVERABLES:\n"
            "- shared/tasks/t-1/workspace/parser.py\n",
        )
        meta = self.read_meta()
        self.assertEqual(meta["status"], "submitted")
        self.assertTrue(meta.get("submitted_at"))

    def test_submit_protocol_files_are_lf_only(self) -> None:
        # Protocol bytes must match Linux copaw exactly: CRLF would ride
        # along to MinIO when written on a Windows dev machine.
        run_cli(self.root, "ack", "t-1")
        code, _, err = self.run_submit()
        self.assertEqual(code, 0, err)
        for name in ("result.md", "meta.json"):
            data = (self.root / "shared" / "tasks" / "t-1" / name).read_bytes()
            self.assertNotIn(b"\r\n", data, f"{name} contains CRLF")

    def test_submit_requires_ack_first(self) -> None:
        code, _, err = self.run_submit()
        self.assertEqual(code, 1)
        self.assertIn("not acknowledged yet", err)
        self.assertEqual(self.read_meta()["status"], "assigned")

    def test_submit_rejects_duplicate(self) -> None:
        run_cli(self.root, "ack", "t-1")
        self.run_submit()
        code, _, err = self.run_submit()
        self.assertEqual(code, 1)
        self.assertIn("cannot submit", err)

    def test_submit_rejects_bad_status(self) -> None:
        run_cli(self.root, "ack", "t-1")
        code, _, err = run_cli(
            self.root, "submit", "t-1", "--status", "MAYBE", "--summary", "x",
        )
        self.assertEqual(code, 1)
        self.assertIn("invalid result status", err)

    def test_submit_rejects_outside_deliverable_path(self) -> None:
        run_cli(self.root, "ack", "t-1")
        code, _, err = run_cli(
            self.root, "submit", "t-1", "--status", "SUCCESS",
            "--summary", "x", "--deliverables", "shared/tasks/t-2/workspace/x.py",
        )
        self.assertEqual(code, 1)
        self.assertIn("deliverable must be under", err)

    # -- identity plumbing ------------------------------------------------

    def test_missing_identity_is_rejected(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--root", str(self.root), "--sync", "none", "check", "t-1"])
        self.assertEqual(code, 1)
        self.assertIn("identity missing", err.getvalue())

    def test_identity_normalizes_matrix_id(self) -> None:
        # Full Matrix ID, display name form and bare localpart must all pass.
        for actor in ("@worker-a:example.org", "worker-a:example.org", "worker-a"):
            code, _, err = run_cli(self.root, "ack", "t-1", actor=actor)
            self.assertEqual(code, 0, err)
            if actor != "@worker-a:example.org":
                # reset state for the next iteration
                meta = self.read_meta()
                meta["status"] = "assigned"
                self.meta_path.write_text(json.dumps(meta, indent=2))


class RecordingBackend(tf_cli.NoSyncBackend):
    """Fake SyncBackend recording every call for contract assertions."""

    def __init__(self, fail_on: frozenset[str] = frozenset()) -> None:
        self.calls: list[tuple] = []
        self.fail_on = fail_on

    def _maybe_fail(self, name: tuple) -> None:
        self.calls.append(name)
        if name[0] in self.fail_on:
            raise RuntimeError(f"simulated {name[0]} failure")

    def pull_task(self, task_id: str) -> None:
        self._maybe_fail(("pull", task_id))

    def push_task(self, task_id: str, exclude: list[str] | None = None) -> None:
        self._maybe_fail(("push", task_id, tuple(exclude or ())))

    def verify_result(self, task_id: str) -> None:
        self._maybe_fail(("verify", task_id))

    def describe(self) -> str:
        return "fake"


def run_with_backend(
    root: Path, backend: RecordingBackend, *argv: str, actor: str = "@worker-a:example.org"
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(tf_cli, "make_sync_backend", return_value=backend), \
         contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(["--root", str(root), "--actor", actor, "--sync", "mc", *argv])
    return code, out.getvalue(), err.getvalue()


class SyncContractTests(unittest.TestCase):
    """Call-order contract vs copaw's taskflow tool (pull/push/verify/exclude)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_workspace(Path(self._tmp.name))
        self.meta_path = self.root / "shared" / "tasks" / "t-1" / "meta.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def read_meta(self) -> dict:
        return json.loads(self.meta_path.read_text())

    def test_check_pulls_then_reads(self) -> None:
        backend = RecordingBackend()
        code, _, err = run_with_backend(self.root, backend, "check", "t-1")
        self.assertEqual(code, 0, err)
        self.assertEqual(backend.calls, [("pull", "t-1")])

    def test_ack_pulls_then_pushes_with_copaw_excludes(self) -> None:
        backend = RecordingBackend()
        code, _, err = run_with_backend(self.root, backend, "ack", "t-1")
        self.assertEqual(code, 0, err)
        self.assertEqual(
            backend.calls,
            [("pull", "t-1"), ("push", "t-1", ("spec.md", "base/"))],
        )

    def test_submit_never_pulls_but_pushes_and_verifies(self) -> None:
        # copaw submit_task does NOT pull first: local transition -> push -> stat.
        backend = RecordingBackend()
        run_with_backend(self.root, backend, "ack", "t-1")
        backend.calls.clear()
        code, _, err = run_with_backend(
            self.root, backend, "submit", "t-1",
            "--status", "SUCCESS", "--summary", "done",
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(
            backend.calls,
            [("push", "t-1", ("spec.md", "base/")), ("verify", "t-1")],
        )


class RollbackTests(unittest.TestCase):
    """Push/verify failure must restore the pre-command local state (§1.3)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_workspace(Path(self._tmp.name))
        self.task_dir = self.root / "shared" / "tasks" / "t-1"
        self.meta_path = self.task_dir / "meta.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def read_meta(self) -> dict:
        return json.loads(self.meta_path.read_text())

    def test_ack_push_failure_rolls_back_to_assigned(self) -> None:
        backend = RecordingBackend(fail_on=frozenset({"push"}))
        code, _, err = run_with_backend(self.root, backend, "ack", "t-1")
        self.assertEqual(code, 1)
        self.assertIn("rolled back to 'assigned'", err)
        self.assertEqual(self.read_meta()["status"], "assigned")

    def test_submit_push_failure_rolls_back_to_in_progress(self) -> None:
        run_with_backend(self.root, RecordingBackend(), "ack", "t-1")
        backend = RecordingBackend(fail_on=frozenset({"push"}))
        code, _, err = run_with_backend(
            self.root, backend, "submit", "t-1",
            "--status", "SUCCESS", "--summary", "done",
        )
        self.assertEqual(code, 1)
        self.assertIn("rolled back to 'in_progress'", err)
        self.assertEqual(self.read_meta()["status"], "in_progress")
        # result.md stays on disk; the retried submit simply rewrites it.
        self.assertTrue((self.task_dir / "result.md").exists())
        # and the retry succeeds once storage is reachable again
        code, out, err = run_with_backend(
            self.root, RecordingBackend(), "submit", "t-1",
            "--status", "SUCCESS", "--summary", "done",
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(self.read_meta()["status"], "submitted")

    def test_submit_verify_failure_rolls_back_to_in_progress(self) -> None:
        run_with_backend(self.root, RecordingBackend(), "ack", "t-1")
        backend = RecordingBackend(fail_on=frozenset({"verify"}))
        code, _, err = run_with_backend(
            self.root, backend, "submit", "t-1",
            "--status", "SUCCESS", "--summary", "done",
        )
        self.assertEqual(code, 1)
        self.assertIn("rolled back to 'in_progress'", err)
        self.assertEqual(self.read_meta()["status"], "in_progress")


if __name__ == "__main__":
    unittest.main()
