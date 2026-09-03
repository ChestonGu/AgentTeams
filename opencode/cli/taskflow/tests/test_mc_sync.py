"""Unit tests for the mc sync backend (T2).

All mc traffic is faked by patching ``mc_sync._mc``; these assert the exact
commands the backend builds, the remote path layout (global vs team shared),
alias handling per runtime mode, and error mapping.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mc_sync  # noqa: E402
from mc_sync import McFileSync, McSyncBackend, McSyncError  # noqa: E402


def make_fs(root: Path, **overrides) -> McFileSync:
    kwargs = dict(
        endpoint="minio:9000",
        access_key="ak",
        secret_key="sk",
        bucket="agentteams-storage",
        worker_name="worker-a",
        local_dir=root,
        shared_dir=root / "shared",
        global_shared_dir=root / "global-shared",
    )
    kwargs.update(overrides)
    return McFileSync(**kwargs)


def fake_mc(records: list, fail: dict[str, str] | None = None):
    def _fake(*args, check=True, warn_on_error=True, log_output=True):
        records.append(args)
        if fail and args and fail.get(args[0]):
            exc = subprocess.CalledProcessError(1, list(args), stderr=fail[args[0]])
            exc.stdout = ""
            raise exc
        return subprocess.CompletedProcess(list(args), 0, stdout="", stderr="")

    return _fake


def no_team() -> mock._patch:
    return mock.patch.object(McFileSync, "_get_worker_info", return_value={"team": ""})


def team(ref: str) -> mock._patch:
    return mock.patch.object(McFileSync, "_get_worker_info", return_value={"team": ref})


class CommandConstructionTests(unittest.TestCase):
    """The exact mc commands per copaw's taskflow tool calls."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.records: list = []
        self.patcher = mock.patch.object(mc_sync, "_mc", fake_mc(self.records))
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.env_patch = mock.patch.dict(os.environ)
        self.env_patch.start()
        os.environ.pop("AGENTTEAMS_RUNTIME", None)  # local/static mode
        self.addCleanup(self.env_patch.stop)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def backend(self) -> McSyncBackend:
        return McSyncBackend(self.root, fs=make_fs(self.root))

    def test_pull_task_mirrors_from_global_shared(self) -> None:
        with no_team():
            backend = self.backend()
            backend.pull_task("t-1")
        expected = (
            "mirror",
            "agentteams/agentteams-storage/shared/tasks/t-1/",
            str(self.root / "shared" / "tasks" / "t-1") + "/",
            "--overwrite",
        )
        self.assertIn(expected, self.records)

    def test_pull_task_uses_team_shared_prefix(self) -> None:
        # Team refs carrying the bucket prefix are stripped of it:
        # "agentteams-storage-alpha" -> "teams/alpha/shared/...".
        with team("agentteams-storage-alpha"):
            backend = self.backend()
            backend.pull_task("t-1")
        expected = (
            "mirror",
            "agentteams/agentteams-storage/teams/alpha/shared/tasks/t-1/",
            str(self.root / "shared" / "tasks" / "t-1") + "/",
            "--overwrite",
        )
        self.assertIn(expected, self.records)

    def test_team_ref_without_bucket_prefix_stays_intact(self) -> None:
        # A team ref that doesn't carry the bucket prefix is used as-is.
        with team("beta"):
            backend = self.backend()
            backend.pull_task("t-1")
        self.assertTrue(
            any("teams/beta/shared/tasks/t-1/" in args[1] for args in self.records),
            self.records,
        )

    def test_env_team_takes_precedence_over_agt(self) -> None:
        # Contract: AGENTTEAMS_TEAM is injected by the orchestrator (storage
        # team name, prefix stripped) — the sandbox has no agt/controller.
        os.environ["AGENTTEAMS_TEAM"] = "gamma"
        try:
            with team("agentteams-storage-somethingelse"):  # agt would say this
                backend = self.backend()
                backend.pull_task("t-1")
        finally:
            del os.environ["AGENTTEAMS_TEAM"]
        self.assertTrue(
            any("teams/gamma/shared/tasks/t-1/" in args[1] for args in self.records),
            self.records,
        )

    def test_empty_env_team_falls_back_to_agt(self) -> None:
        os.environ["AGENTTEAMS_TEAM"] = ""
        try:
            with team("agentteams-storage-alpha"):
                backend = self.backend()
                backend.pull_task("t-1")
        finally:
            del os.environ["AGENTTEAMS_TEAM"]
        self.assertTrue(
            any("teams/alpha/shared/tasks/t-1/" in args[1] for args in self.records),
            self.records,
        )

    def test_push_task_applies_copaw_excludes_by_default(self) -> None:
        task_dir = self.root / "shared" / "tasks" / "t-1"
        task_dir.mkdir(parents=True)
        with no_team():
            backend = self.backend()
            backend.push_task("t-1")
        expected = (
            "mirror",
            str(task_dir) + "/",
            "agentteams/agentteams-storage/shared/tasks/t-1/",
            "--overwrite",
            "--exclude", "spec.md",
            "--exclude", "base/",
        )
        self.assertIn(expected, self.records)

    def test_verify_result_stats_result_md(self) -> None:
        with no_team():
            backend = self.backend()
            backend.verify_result("t-1")
        self.assertIn(
            ("stat", "agentteams/agentteams-storage/shared/tasks/t-1/result.md"),
            self.records,
        )

    def test_local_mode_sets_static_alias_once(self) -> None:
        with no_team():
            backend = self.backend()
            backend.pull_task("t-1")
            backend.verify_result("t-1")
        alias_sets = [args for args in self.records if args[:2] == ("alias", "set")]
        self.assertEqual(len(alias_sets), 1)
        self.assertEqual(
            alias_sets[0],
            ("alias", "set", "agentteams", "http://minio:9000", "ak", "sk"),
        )

    def test_k8s_mode_never_sets_alias(self) -> None:
        os.environ["AGENTTEAMS_RUNTIME"] = "k8s"
        with no_team():
            backend = self.backend()
            backend.pull_task("t-1")
        self.assertFalse(any(args[:2] == ("alias", "set") for args in self.records))


class ErrorMappingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.env_patch = mock.patch.dict(os.environ)
        self.env_patch.start()
        os.environ.pop("AGENTTEAMS_RUNTIME", None)
        self.addCleanup(self.env_patch.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_pull_missing_object_maps_to_taskflow_error(self) -> None:
        records: list = []
        patcher = mock.patch.object(
            mc_sync, "_mc", fake_mc(records, fail={"mirror": "Object does not exist"})
        )
        with patcher, no_team():
            backend = McSyncBackend(self.root, fs=make_fs(self.root))
            with self.assertRaises(McSyncError) as ctx:
                backend.pull_task("t-1")
        self.assertIn("not found in shared storage", str(ctx.exception))

    def test_pull_other_mc_failure_propagates(self) -> None:
        records: list = []
        patcher = mock.patch.object(
            mc_sync, "_mc", fake_mc(records, fail={"mirror": "connection refused"})
        )
        with patcher, no_team():
            backend = McSyncBackend(self.root, fs=make_fs(self.root))
            with self.assertRaises(subprocess.CalledProcessError):
                backend.pull_task("t-1")


class FromEnvTests(unittest.TestCase):
    """McSyncBackend env validation per the §4.1 contract."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_missing_worker_name_rejected(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(McSyncError) as ctx:
                McSyncBackend(self.root)
        self.assertIn("AGENTTEAMS_WORKER_NAME", str(ctx.exception))

    def test_local_mode_requires_static_trio(self) -> None:
        env = {"AGENTTEAMS_WORKER_NAME": "worker-a", "AGENTTEAMS_FS_ENDPOINT": "minio:9000"}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(McSyncError) as ctx:
                McSyncBackend(self.root)
        self.assertIn("ACCESS_KEY", str(ctx.exception))

    def test_k8s_mode_builds_without_static_trio(self) -> None:
        env = {
            "AGENTTEAMS_WORKER_NAME": "worker-a",
            "AGENTTEAMS_RUNTIME": "k8s",
            "AGENTTEAMS_FS_BUCKET": "agentteams-storage",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            backend = McSyncBackend(self.root)  # must not raise
        self.assertEqual(backend._fs.bucket, "agentteams-storage")

    def test_full_local_env_builds(self) -> None:
        env = {
            "AGENTTEAMS_WORKER_NAME": "worker-a",
            "AGENTTEAMS_FS_ENDPOINT": "http://minio:9000",
            "AGENTTEAMS_FS_ACCESS_KEY": "ak",
            "AGENTTEAMS_FS_SECRET_KEY": "sk",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            backend = McSyncBackend(self.root)
        self.assertEqual(backend._fs.worker_name, "worker-a")
        self.assertEqual(backend._fs.bucket, "agentteams-storage")  # default bucket


if __name__ == "__main__":
    unittest.main()
