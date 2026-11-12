"""Tests for the project-watch register hook (independent from STATUS_EVENT)."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import urllib.request

import copaw_worker.hooks.tools.project_register as project_register
from copaw_worker.hooks.tools.project_register import register_project_watch


def _tool_response(payload: dict) -> SimpleNamespace:
    return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(payload))])


def _plan_dag_ok() -> SimpleNamespace:
    return _tool_response({
        "ok": True,
        "action": "plan_dag",
        "tasks": [{"task_id": "t-1", "title": "Node A", "status": "pending"}],
        "readyNodes": [{"task_id": "t-1", "title": "Node A", "status": "pending"}],
    })


@pytest.fixture(autouse=True)
def _isolated_runtime(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("COPAW_WORKING_DIR", str(tmp_path / "worker" / "copaw-worker"))
    monkeypatch.delenv("AGENTTEAMS_WATCHDOG_REGISTER_URL", raising=False)
    monkeypatch.delenv("AGENTTEAMS_STATUS_WEBHOOK_URL", raising=False)


@pytest.mark.asyncio
async def test_plan_dag_success_posts_register(monkeypatch):
    calls: list[urllib.request.Request] = []

    def fake_urlopen(request, timeout=None):
        calls.append(request)
        response = MagicMock()
        response.__enter__ = MagicMock(return_value=response)
        response.__exit__ = MagicMock(return_value=False)
        response.status = 200
        return response

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("AGENTTEAMS_WATCHDOG_REGISTER_URL", "http://watcher:9090/register")

    await register_project_watch(
        "projectflow", "plan_dag",
        payload={"projectId": "p-1"},
        result=_plan_dag_ok(),
    )

    assert len(calls) == 1
    request = calls[0]
    assert request.full_url == "http://watcher:9090/register"
    body = json.loads(request.data.decode("utf-8"))
    assert body["event"] == "projectflow.plan_dag"
    assert body["projectId"] == "p-1"
    assert body["tasks"][0]["task_id"] == "t-1"


@pytest.mark.asyncio
async def test_register_url_empty_no_post(monkeypatch):
    calls: list[urllib.request.Request] = []

    def fake_urlopen(request, timeout=None):
        calls.append(request)
        return MagicMock()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    await register_project_watch(
        "projectflow", "plan_dag",
        payload={"projectId": "p-1"},
        result=_plan_dag_ok(),
    )

    assert calls == []


@pytest.mark.asyncio
async def test_create_project_never_registers(monkeypatch):
    calls: list[urllib.request.Request] = []

    def fake_urlopen(request, timeout=None):
        calls.append(request)
        return MagicMock()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("AGENTTEAMS_WATCHDOG_REGISTER_URL", "http://watcher:9090/register")

    await register_project_watch(
        "projectflow", "create_project",
        payload={"projectId": "p-1"},
        result=_tool_response({"ok": True, "project": {"projectId": "p-1"}}),
    )

    assert calls == []


@pytest.mark.asyncio
async def test_dry_run_never_registers(monkeypatch):
    calls: list[urllib.request.Request] = []

    def fake_urlopen(request, timeout=None):
        calls.append(request)
        return MagicMock()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("AGENTTEAMS_WATCHDOG_REGISTER_URL", "http://watcher:9090/register")

    await register_project_watch(
        "projectflow", "plan_dag",
        payload={"projectId": "p-1"},
        result=_tool_response({"ok": True, "dryRun": True}),
    )

    assert calls == []


@pytest.mark.asyncio
async def test_failed_result_never_registers(monkeypatch):
    calls: list[urllib.request.Request] = []

    def fake_urlopen(request, timeout=None):
        calls.append(request)
        return MagicMock()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("AGENTTEAMS_WATCHDOG_REGISTER_URL", "http://watcher:9090/register")

    await register_project_watch(
        "projectflow", "plan_dag",
        payload={"projectId": "p-1"},
        result=_tool_response({"ok": False, "error": "boom"}),
    )

    assert calls == []


@pytest.mark.asyncio
async def test_direct_mode_never_registers(monkeypatch, tmp_path):
    calls: list[urllib.request.Request] = []
    runtime_dir = tmp_path / "worker" / "runtime"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "runtime.yaml").write_text(
        "kind: MemberRuntimeConfig\nmember:\n  role: worker\nteam:\n"
        "  collaboration: direct\n",
        encoding="utf-8",
    )
    working_dir = tmp_path / "worker" / "copaw-worker"
    working_dir.mkdir(parents=True, exist_ok=True)

    def fake_urlopen(request, timeout=None):
        calls.append(request)
        return MagicMock()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("COPAW_WORKING_DIR", str(working_dir))
    monkeypatch.setenv("AGENTTEAMS_WATCHDOG_REGISTER_URL", "http://watcher:9090/register")

    await register_project_watch(
        "projectflow", "plan_dag",
        payload={"projectId": "p-1"},
        result=_plan_dag_ok(),
    )

    assert calls == []


@pytest.mark.asyncio
async def test_register_failure_warns_not_raises(monkeypatch, caplog):
    def fake_urlopen(request, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("AGENTTEAMS_WATCHDOG_REGISTER_URL", "http://watcher:9090/register")

    # Must not raise.
    await register_project_watch(
        "projectflow", "plan_dag",
        payload={"projectId": "p-1"},
        result=_plan_dag_ok(),
    )

    assert any(
        "project register delivery failed" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_watchdog_file_config_fallback(monkeypatch, tmp_path):
    calls: list[urllib.request.Request] = []
    shared_config = (
        tmp_path / "worker" / "copaw-worker" / "workspaces" / "default"
        / "shared" / "config"
    )
    shared_config.mkdir(parents=True)
    (shared_config / "watchdog-register.json").write_text(
        json.dumps({"AGENTTEAMS_WATCHDOG_REGISTER_URL": "http://file:9090/register"}),
        encoding="utf-8",
    )

    def fake_urlopen(request, timeout=None):
        calls.append(request)
        response = MagicMock()
        response.__enter__ = MagicMock(return_value=response)
        response.__exit__ = MagicMock(return_value=False)
        response.status = 200
        return response

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    await register_project_watch(
        "projectflow", "plan_dag",
        payload={"projectId": "p-1"},
        result=_plan_dag_ok(),
    )

    assert len(calls) == 1
    assert calls[0].full_url == "http://file:9090/register"


@pytest.mark.asyncio
async def test_env_overrides_watchdog_file_config(monkeypatch, tmp_path):
    calls: list[urllib.request.Request] = []
    shared_config = (
        tmp_path / "worker" / "copaw-worker" / "workspaces" / "default"
        / "shared" / "config"
    )
    shared_config.mkdir(parents=True)
    (shared_config / "watchdog-register.json").write_text(
        json.dumps({"AGENTTEAMS_WATCHDOG_REGISTER_URL": "http://file:9090/register"}),
        encoding="utf-8",
    )

    def fake_urlopen(request, timeout=None):
        calls.append(request)
        response = MagicMock()
        response.__enter__ = MagicMock(return_value=response)
        response.__exit__ = MagicMock(return_value=False)
        response.status = 200
        return response

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("AGENTTEAMS_WATCHDOG_REGISTER_URL", "http://env:9090/register")

    await register_project_watch(
        "projectflow", "plan_dag",
        payload={"projectId": "p-1"},
        result=_plan_dag_ok(),
    )

    assert len(calls) == 1
    assert calls[0].full_url == "http://env:9090/register"


@pytest.mark.asyncio
async def test_status_webhook_config_does_not_trigger_register(monkeypatch, tmp_path):
    """Register reads watchdog-register.json, not webhook.json."""
    calls: list[urllib.request.Request] = []
    shared_config = (
        tmp_path / "worker" / "copaw-worker" / "workspaces" / "default"
        / "shared" / "config"
    )
    shared_config.mkdir(parents=True)
    # Only the STATUS webhook config exists — no watchdog-register.json.
    (shared_config / "webhook.json").write_text(
        json.dumps({"AGENTTEAMS_STATUS_WEBHOOK_URL": "http://sidecar:9099/status"}),
        encoding="utf-8",
    )

    def fake_urlopen(request, timeout=None):
        calls.append(request)
        return MagicMock()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("AGENTTEAMS_STATUS_WEBHOOK_URL", "http://sidecar:9099/status")

    await register_project_watch(
        "projectflow", "plan_dag",
        payload={"projectId": "p-1"},
        result=_plan_dag_ok(),
    )

    assert calls == []
