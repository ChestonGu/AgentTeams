"""Unit tests for the agentteams-sync CLI (T3).

mc traffic is faked by patching ``mc_sync._mc``; tests assert the JSON
payload shape copaw's filesync tool returned, the directory-path
normalization rules, exclude plumbing, and error mapping.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "taskflow"))

import mc_sync  # noqa: E402
from mc_sync import McFileSync  # noqa: E402
import agentteams_sync as sync_cli  # noqa: E402


ENV = {
    "AGENTTEAMS_WORKER_NAME": "worker-a",
    "AGENTTEAMS_FS_ENDPOINT": "minio:9000",
    "AGENTTEAMS_FS_ACCESS_KEY": "ak",
    "AGENTTEAMS_FS_SECRET_KEY": "sk",
}


def fake_mc(records: list, fail: dict[str, str] | None = None, outputs: dict[str, str] | None = None):
    def _fake(*args, check=True, warn_on_error=True, log_output=True):
        records.append(args)
        if fail and args and fail.get(args[0]):
            exc = subprocess.CalledProcessError(1, list(args), stderr=fail[args[0]])
            exc.stdout = ""
            raise exc
        stdout = (outputs or {}).get(args[0], "")
        return subprocess.CompletedProcess(list(args), 0, stdout=stdout, stderr="")

    return _fake


def run_cli(root: Path, *argv: str) -> tuple[int, dict, str]:
    """Run the CLI with stdout captured; returns (exit, parsed JSON, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = sync_cli.main(["--root", str(root), *argv])
    return code, json.loads(out.getvalue()), err.getvalue()


class AgentTeamsSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.records: list = []
        self.mc_patcher = mock.patch.object(mc_sync, "_mc", fake_mc(self.records))
        self.mc_patcher.start()
        self.addCleanup(self.mc_patcher.stop)
        self.team_patcher = mock.patch.object(
            McFileSync, "_get_worker_info", return_value={"team": ""}
        )
        self.team_patcher.start()
        self.addCleanup(self.team_patcher.stop)
        self.env_patcher = mock.patch.dict(os.environ, ENV, clear=True)
        self.env_patcher.start()
        os.environ.pop("AGENTTEAMS_RUNTIME", None)  # local/static mode
        self.addCleanup(self.env_patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    # -- normalization ---------------------------------------------------

    def test_pull_task_dir_path_gets_trailing_slash(self) -> None:
        code, payload, _ = run_cli(self.root, "pull", "shared/tasks/t-1")
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["path"], "shared/tasks/t-1/")
        self.assertEqual(payload["kind"], "shared")
        self.assertEqual(payload["localPath"], str(self.root / "shared" / "tasks" / "t-1"))
        self.assertIn(
            ("mirror",
             "agentteams/agentteams-storage/shared/tasks/t-1/",
             str(self.root / "shared" / "tasks" / "t-1") + "/",
             "--overwrite"),
            self.records,
        )

    def test_stat_never_normalizes(self) -> None:
        code, payload, _ = run_cli(self.root, "stat", "shared/tasks/t-1/result.md")
        self.assertEqual(code, 0)
        self.assertTrue(payload["exists"])
        self.assertIn(
            ("stat", "agentteams/agentteams-storage/shared/tasks/t-1/result.md"),
            self.records,
        )
        # a bare 3-part path stays verbatim for stat (exact object match)
        self.records.clear()
        code, payload, _ = run_cli(self.root, "stat", "shared/tasks/t-1")
        self.assertEqual(payload["path"], "shared/tasks/t-1")

    def test_deep_file_path_is_not_normalized(self) -> None:
        run_cli(self.root, "pull", "shared/tasks/t-1/workspace/out.py")
        self.assertIn(
            ("cp",
             "agentteams/agentteams-storage/shared/tasks/t-1/workspace/out.py",
             str(self.root / "shared" / "tasks" / "t-1" / "workspace" / "out.py")),
            self.records,
        )

    # -- push ----------------------------------------------------------------

    def test_push_passes_excludes(self) -> None:
        task_dir = self.root / "shared" / "tasks" / "t-1"
        task_dir.mkdir(parents=True)
        code, payload, _ = run_cli(
            self.root, "push", "shared/tasks/t-1",
            "--exclude", "spec.md", "--exclude", "base/",
        )
        self.assertEqual(code, 0)
        self.assertTrue(payload["pushed"])
        self.assertEqual(payload["exclude"], ["spec.md", "base/"])
        self.assertIn(
            ("mirror", str(task_dir) + "/",
             "agentteams/agentteams-storage/shared/tasks/t-1/",
             "--overwrite", "--exclude", "spec.md", "--exclude", "base/"),
            self.records,
        )

    def test_push_global_shared_is_read_only(self) -> None:
        shared = self.root / "global-shared" / "docs"
        shared.mkdir(parents=True)
        code, payload, _ = run_cli(self.root, "push", "global-shared/docs/")
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertIn("read-only", payload["error"])

    # -- list ----------------------------------------------------------------

    def test_list_returns_entries(self) -> None:
        self.mc_patcher.stop()
        listing = "[2026-09-01 00:00:00 UTC]  1.2KiB spec.md\n[2026-09-01 00:00:00 UTC]  300B meta.json\n"
        self.mc_patcher = mock.patch.object(
            mc_sync, "_mc", fake_mc(self.records, outputs={"ls": listing})
        )
        self.mc_patcher.start()
        code, payload, _ = run_cli(self.root, "list", "shared/tasks/t-1")
        self.assertEqual(code, 0)
        self.assertEqual(len(payload["entries"]), 2)
        self.assertIn("spec.md", payload["entries"][0])

    # -- dry-run / errors ------------------------------------------------------

    def test_dry_run_touches_no_storage(self) -> None:
        code, payload, _ = run_cli(self.root, "pull", "shared/tasks/t-1", "--dry-run")
        self.assertEqual(code, 0)
        self.assertTrue(payload["dryRun"])
        self.assertEqual(payload["path"], "shared/tasks/t-1/")
        self.assertEqual(self.records, [])  # not even alias set

    def test_missing_env_reports_clean_json_error(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            code, payload, _ = run_cli(self.root, "stat", "shared/x.md")
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertIn("AGENTTEAMS_WORKER_NAME", payload["error"])

    def test_invalid_path_reports_clean_json_error(self) -> None:
        code, payload, _ = run_cli(self.root, "pull", "workspace/foo.txt")
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertIn("path must start with shared/", payload["error"])

    def test_stat_missing_object_reports_not_exists(self) -> None:
        self.mc_patcher.stop()
        self.mc_patcher = mock.patch.object(
            mc_sync, "_mc", fake_mc(self.records, fail={"stat": "Object does not exist"})
        )
        self.mc_patcher.start()
        code, payload, _ = run_cli(self.root, "stat", "shared/tasks/t-1/result.md")
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertIn("Object does not exist", payload["error"])


if __name__ == "__main__":
    unittest.main()
