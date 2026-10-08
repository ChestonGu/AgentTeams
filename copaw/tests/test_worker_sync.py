"""Tests for CoPaw worker file sync behavior."""

import logging
import subprocess

from copaw_worker import sync
from copaw_worker.sync import FileSync


def test_ensure_alias_skips_static_alias_in_k8s_mode(monkeypatch, tmp_path):
    calls = []

    monkeypatch.setenv("AGENTTEAMS_RUNTIME", "k8s")
    monkeypatch.setattr(sync, "_mc", lambda *args, **_kwargs: calls.append(args))

    fs = FileSync(
        endpoint="minio:9000",
        access_key="tt",
        secret_key="secret",
        bucket="agentteams",
        worker_name="tt",
        local_dir=tmp_path,
    )

    fs._ensure_alias()

    assert fs._alias_set is True
    assert calls == []


def test_filesync_fallback_uses_copaw_working_dir_parent(monkeypatch, tmp_path):
    working_dir = tmp_path / "alice" / ".copaw"
    monkeypatch.setenv("COPAW_WORKING_DIR", str(working_dir))

    fs = FileSync(
        endpoint="minio:9000",
        access_key="tt",
        secret_key="secret",
        bucket="agentteams",
        worker_name="alice",
    )

    assert fs.local_dir == tmp_path / "alice"


def test_cat_missing_object_is_debug_only(monkeypatch, tmp_path, caplog):
    fs = FileSync(
        endpoint="minio:9000",
        access_key="tt",
        secret_key="secret",
        bucket="agentteams",
        worker_name="tt",
        local_dir=tmp_path,
    )
    monkeypatch.setattr(fs, "_ensure_alias", lambda: None)
    monkeypatch.setattr(
        sync,
        "_mc",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            _args,
            1,
            stdout="",
            stderr="mc.bin: <ERROR> Object does not exist.",
        ),
    )
    caplog.set_level(logging.WARNING)

    assert fs._cat("agents/tt/config/mcporter.json") is None
    assert "Object does not exist" not in caplog.text


def test_cat_non_missing_failure_warns(monkeypatch, tmp_path, caplog):
    fs = FileSync(
        endpoint="minio:9000",
        access_key="tt",
        secret_key="secret",
        bucket="agentteams",
        worker_name="tt",
        local_dir=tmp_path,
    )
    monkeypatch.setattr(fs, "_ensure_alias", lambda: None)
    monkeypatch.setattr(
        sync,
        "_mc",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            _args,
            1,
            stdout="",
            stderr="AccessDenied: denied",
        ),
    )
    caplog.set_level(logging.WARNING)

    assert fs._cat("agents/tt/openclaw.json") is None
    assert "mc cat failed" in caplog.text
    assert "AccessDenied: denied" in caplog.text


def test_on_files_pulled_projects_agents_md_into_runtime(tmp_path):
    """AGENTS.md changes pulled from MinIO must reach the CoPaw workspace.

    Regression guard for the stale-coordinator bug: workers that start as
    standalone and are later added to a team kept their first-boot AGENTS.md
    (Coordinator: @manager) forever, so TASK_COMPLETED mentions went to a
    user outside the Team Room and the Team Leader never saw them.
    """
    import asyncio
    from types import SimpleNamespace

    from copaw_worker.worker import Worker

    standard = tmp_path / "standard"
    standard.mkdir()
    (standard / "AGENTS.md").write_text("team coordination block\n")
    (standard / "SOUL.md").write_text("soul\n")

    runtime_dir = standard / ".copaw"

    stub = SimpleNamespace(
        sync=SimpleNamespace(local_dir=standard, list_skills=lambda: []),
        _copaw_working_dir=runtime_dir,
    )

    asyncio.run(Worker._on_files_pulled(stub, ["AGENTS.md"]))

    workspace = runtime_dir / "workspaces" / "default"
    assert (workspace / "AGENTS.md").read_text() == "team coordination block\n"
    assert (workspace / "SOUL.md").read_text() == "soul\n"
    # HEARTBEAT is first-boot only — nothing here to copy, none created.
    assert not (workspace / "HEARTBEAT.md").exists()
