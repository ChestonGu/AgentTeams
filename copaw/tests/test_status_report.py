"""Tests for the fire-and-forget status reporting hook."""

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import urllib.request

import copaw_worker.hooks.tools.status_report as status_report
from copaw_worker.hooks.tools.status_report import report_tool_event


def _tool_response(payload: dict) -> SimpleNamespace:
    """Duck-typed ToolResponse: content[0].text holds the JSON payload."""
    return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(payload))])


def _logged_events(caplog) -> list[dict]:
    events = []
    for record in caplog.records:
        if record.message.startswith("STATUS_EVENT "):
            events.append(json.loads(record.message[len("STATUS_EVENT ") :]))
    return events


@pytest.fixture(autouse=True)
def _isolated_runtime(monkeypatch, tmp_path: Path) -> None:
    """Point runtime lookup at an empty tmp dir; no webhook by default."""
    monkeypatch.setenv("COPAW_WORKING_DIR", str(tmp_path / "worker" / "copaw-worker"))
    monkeypatch.delenv("AGENTTEAMS_STATUS_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("AGENTTEAMS_STATUS_WEBHOOK_TOKEN", raising=False)
    monkeypatch.delenv("QWENPAW_WORKING_DIR", raising=False)


@pytest.mark.asyncio
async def test_state_change_logs_event(caplog):
    with caplog.at_level(logging.INFO, logger=status_report.logger.name):
        await report_tool_event(
            "projectflow",
            "create_project",
            payload={"projectId": "p-1", "title": "Demo"},
            result=_tool_response(
                {"ok": True, "action": "create_project", "project": {"projectId": "p-1", "status": "active"}}
            ),
        )
    events = _logged_events(caplog)
    assert len(events) == 1
    event = events[0]
    assert event["event"] == "projectflow.create_project"
    assert event["projectId"] == "p-1"
    assert event["projectStatus"] == "active"
    assert event["project"]["projectId"] == "p-1"
    assert event["timestamp"]


@pytest.mark.asyncio
async def test_delegate_event_embeds_full_task_meta(caplog):
    with caplog.at_level(logging.INFO, logger=status_report.logger.name):
        await report_tool_event(
            "taskflow",
            "delegate_task",
            payload={"projectId": "p-1", "taskId": "t-1"},
            result=_tool_response(
                {
                    "ok": True,
                    "action": "delegate_task",
                    "task": {
                        "task_id": "t-1",
                        "project_id": "p-1",
                        "task_title": "撰写初稿",
                        "assigned_to": "dev-1",
                        "room_id": "room:!r:dom",
                        "status": "assigned",
                        "depends_on": ["t-0"],
                        "assigned_at": "2026-10-22T00:00:00Z",
                    },
                },
            ),
        )
    event = _logged_events(caplog)[0]
    task = event["task"]
    assert task["task_title"] == "撰写初稿"
    assert task["assigned_to"] == "dev-1"
    assert task["room_id"] == "room:!r:dom"
    assert task["depends_on"] == ["t-0"]
    assert task["assigned_at"] == "2026-10-22T00:00:00Z"


@pytest.mark.asyncio
async def test_plan_dag_event_carries_full_node_graph(caplog):
    nodes = [
        {"task_id": "t-1", "title": "调研", "assigned_to": "dev", "depends_on": [], "status": "pending"},
        {"task_id": "t-2", "title": "评审", "assigned_to": "qa", "depends_on": ["t-1"], "status": "pending"},
    ]
    with caplog.at_level(logging.INFO, logger=status_report.logger.name):
        await report_tool_event(
            "projectflow",
            "plan_dag",
            payload={"projectId": "p-1"},
            result=_tool_response(
                {"ok": True, "action": "plan_dag", "tasks": nodes, "readyNodes": nodes[:1]}
            ),
        )
    event = _logged_events(caplog)[0]
    assert event["tasks"] == nodes
    assert event["readyNodes"] == nodes[:1]
    assert event["tasks"][0]["status"] == "pending"


@pytest.mark.asyncio
async def test_snapshot_and_project_meta_from_local_store(monkeypatch, tmp_path, caplog):
    working_dir = tmp_path / "worker" / "copaw-worker"
    shared = working_dir / "workspaces" / "default" / "shared"
    (shared / "projects" / "p-1").mkdir(parents=True)
    (shared / "projects" / "p-1" / "meta.json").write_text(
        json.dumps({"project_id": "p-1", "title": "演示项目", "status": "active"}), encoding="utf-8"
    )
    for tid, status in (("t-1", "submitted"), ("t-2", "assigned")):
        (shared / "tasks" / tid).mkdir(parents=True)
        (shared / "tasks" / tid / "meta.json").write_text(
            json.dumps(
                {
                    "task_id": tid,
                    "project_id": "p-1",
                    "task_title": f"任务{tid}",
                    "assigned_to": "dev",
                    "status": status,
                    "depends_on": [],
                }
            ),
            encoding="utf-8",
        )
    (shared / "tasks" / "t-9").mkdir()
    (shared / "tasks" / "t-9" / "meta.json").write_text(
        json.dumps({"task_id": "t-9", "project_id": "p-other", "task_title": "无关", "status": "assigned"}),
        encoding="utf-8",
    )
    with caplog.at_level(logging.INFO, logger=status_report.logger.name):
        await report_tool_event(
            "taskflow",
            "ack_task",
            payload={"taskId": "t-2"},
            result=_tool_response(
                {"ok": True, "task": {"task_id": "t-2", "project_id": "p-1", "status": "in_progress"}}
            ),
        )
    event = _logged_events(caplog)[0]
    # project block + projectStatus backfilled from the local store copy.
    assert event["project"]["title"] == "演示项目"
    assert event["projectStatus"] == "active"
    # snapshot covers both p-1 tasks, not the p-other one, sorted by id.
    assert [row["taskId"] for row in event["projectTasks"]] == ["t-1", "t-2"]
    assert event["projectTasks"][0]["status"] == "submitted"


@pytest.mark.asyncio
async def test_submit_event_embeds_result(caplog):
    with caplog.at_level(logging.INFO, logger=status_report.logger.name):
        await report_tool_event(
            "taskflow",
            "submit_task",
            payload={"taskId": "t-1"},
            result=_tool_response(
                {
                    "ok": True,
                    "task": {"task_id": "t-1", "project_id": "p-1", "status": "submitted"},
                    "result": {"status": "SUCCESS", "summary": "完成", "deliverables": ["d.md"]},
                }
            ),
        )
    event = _logged_events(caplog)[0]
    assert event["result"] == {"status": "SUCCESS", "summary": "完成", "deliverables": ["d.md"]}


@pytest.mark.asyncio
async def test_taskflow_delegate_reports_task_fields(caplog):
    with caplog.at_level(logging.INFO, logger=status_report.logger.name):
        await report_tool_event(
            "taskflow",
            "delegate_task",
            payload={"projectId": "p-1", "taskId": "t-1"},
            result=_tool_response(
                {
                    "ok": True,
                    "action": "delegate_task",
                    "task": {"task_id": "t-1", "status": "assigned"},
                }
            ),
        )
    events = _logged_events(caplog)
    assert len(events) == 1
    assert events[0]["taskId"] == "t-1"
    assert events[0]["taskStatus"] == "assigned"


@pytest.mark.asyncio
async def test_readonly_action_not_reported(caplog):
    with caplog.at_level(logging.INFO, logger=status_report.logger.name):
        await report_tool_event(
            "projectflow",
            "ready_nodes",
            payload={"projectId": "p-1"},
            result=_tool_response({"ok": True, "action": "ready_nodes"}),
        )
        await report_tool_event(
            "projectflow",
            "check_active_tasks",
            result=_tool_response({"ok": True, "action": "check_active_tasks"}),
        )
    assert _logged_events(caplog) == []


@pytest.mark.asyncio
async def test_dryrun_and_failure_not_reported(caplog):
    with caplog.at_level(logging.INFO, logger=status_report.logger.name):
        await report_tool_event(
            "projectflow",
            "create_project",
            payload={"projectId": "p-1"},
            result=_tool_response({"ok": True, "dryRun": True, "action": "create_project"}),
        )
        await report_tool_event(
            "taskflow",
            "submit_task",
            payload={"taskId": "t-1"},
            result=_tool_response({"ok": False, "error": "boom"}),
        )
    assert _logged_events(caplog) == []


@pytest.mark.asyncio
async def test_report_never_raises(caplog):
    with caplog.at_level(logging.INFO, logger=status_report.logger.name):
        # Malformed result content must not propagate into the tool call.
        await report_tool_event(
            "projectflow",
            "create_project",
            payload={"projectId": "p-1"},
            result=SimpleNamespace(content=[SimpleNamespace(text="not-json")]),
        )
    assert _logged_events(caplog) == []


@pytest.mark.asyncio
async def test_team_name_read_from_runtime_yaml(monkeypatch, tmp_path, caplog):
    working_dir = tmp_path / "worker" / "copaw-worker"
    runtime_dir = working_dir.parent / "runtime"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "runtime.yaml").write_text(
        "kind: MemberRuntimeConfig\n"
        "team:\n"
        "  name: t-status\n"
        "  teamRoomId: \"!room:domain\"\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTTEAMS_MATRIX_USER_ID", "@leader:domain")
    with caplog.at_level(logging.INFO, logger=status_report.logger.name):
        await report_tool_event(
            "projectflow",
            "complete_project",
            payload={"projectId": "p-1"},
            result=_tool_response({"ok": True, "project": {"projectId": "p-1", "status": "completed"}}),
        )
    events = _logged_events(caplog)
    assert events[0]["team"] == "t-status"
    assert events[0]["actor"] == "@leader:domain"
    assert events[0]["projectStatus"] == "completed"


@pytest.mark.asyncio
async def test_actor_resolved_from_agent_json(monkeypatch, tmp_path, caplog):
    working_dir = tmp_path / "worker" / "copaw-worker"
    workspace = working_dir / "workspaces" / "default"
    workspace.mkdir(parents=True)
    (workspace / "agent.json").write_text(
        json.dumps({"channels": {"matrix": {"user_id": "@dev-x:domain"}}}),
        encoding="utf-8",
    )
    monkeypatch.delenv("AGENTTEAMS_MATRIX_USER_ID", raising=False)
    monkeypatch.delenv("COPAW_MATRIX_USER_ID", raising=False)
    with caplog.at_level(logging.INFO, logger=status_report.logger.name):
        await report_tool_event(
            "taskflow",
            "ack_task",
            payload={"taskId": "t-1"},
            result=_tool_response({"ok": True, "task": {"task_id": "t-1", "status": "in_progress"}}),
        )
    events = _logged_events(caplog)
    assert events[0]["actor"] == "@dev-x:domain"


@pytest.mark.asyncio
async def test_webhook_posted_with_bearer_token(monkeypatch, caplog):
    request_calls: list[urllib.request.Request] = []

    def fake_urlopen(request, timeout=None):
        request_calls.append(request)
        response = MagicMock()
        response.__enter__ = MagicMock(return_value=response)
        response.__exit__ = MagicMock(return_value=False)
        response.status = 200
        return response

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("AGENTTEAMS_STATUS_WEBHOOK_URL", "http://sidecar:9099/status")
    monkeypatch.setenv("AGENTTEAMS_STATUS_WEBHOOK_TOKEN", "secret-token")
    with caplog.at_level(logging.INFO, logger=status_report.logger.name):
        await report_tool_event(
            "projectflow",
            "create_project",
            payload={"projectId": "p-1"},
            result=_tool_response({"ok": True, "project": {"projectId": "p-1"}}),
        )
    assert len(request_calls) == 1
    request = request_calls[0]
    assert request.full_url == "http://sidecar:9099/status"
    assert request.get_header("Authorization") == "Bearer secret-token"
    body = json.loads(request.data.decode("utf-8"))
    assert body["event"] == "projectflow.create_project"
    # The log line is still emitted alongside the webhook delivery.
    assert len(_logged_events(caplog)) == 1


@pytest.mark.asyncio
async def test_webhook_failure_swallowed(monkeypatch, caplog):
    def fake_urlopen(request, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("AGENTTEAMS_STATUS_WEBHOOK_URL", "http://sidecar:9099/status")
    with caplog.at_level(logging.INFO, logger=status_report.logger.name):
        # Must not raise despite the webhook being unreachable.
        await report_tool_event(
            "projectflow",
            "create_project",
            payload={"projectId": "p-1"},
            result=_tool_response({"ok": True, "project": {"projectId": "p-1"}}),
        )
    assert len(_logged_events(caplog)) == 1


def _write_webhook_config(monkeypatch, tmp_path: Path, config: dict) -> Path:
    working_dir = tmp_path / "worker" / "copaw-worker"
    config_dir = working_dir / "workspaces" / "default" / "shared" / "config"
    config_dir.mkdir(parents=True)
    path = config_dir / "webhook.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("COPAW_WORKING_DIR", str(working_dir))
    return path


@pytest.mark.asyncio
async def test_webhook_url_and_token_from_shared_config_file(monkeypatch, tmp_path, caplog):
    request_calls: list[urllib.request.Request] = []

    def fake_urlopen(request, timeout=None):
        request_calls.append(request)
        response = MagicMock()
        response.__enter__ = MagicMock(return_value=response)
        response.__exit__ = MagicMock(return_value=False)
        response.status = 200
        return response

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    _write_webhook_config(
        monkeypatch,
        tmp_path,
        {
            "AGENTTEAMS_STATUS_WEBHOOK_URL": "http://file-sink:9099/status",
            "AGENTTEAMS_STATUS_WEBHOOK_TOKEN": "file-token",
        },
    )
    with caplog.at_level(logging.INFO, logger=status_report.logger.name):
        await report_tool_event(
            "projectflow",
            "create_project",
            payload={"projectId": "p-1"},
            result=_tool_response({"ok": True, "project": {"projectId": "p-1"}}),
        )
    assert len(request_calls) == 1
    request = request_calls[0]
    assert request.full_url == "http://file-sink:9099/status"
    assert request.get_header("Authorization") == "Bearer file-token"
    # The log line is still emitted alongside the webhook delivery.
    assert len(_logged_events(caplog)) == 1


@pytest.mark.asyncio
async def test_webhook_env_wins_over_shared_config_file(monkeypatch, tmp_path, caplog):
    request_calls: list[urllib.request.Request] = []

    def fake_urlopen(request, timeout=None):
        request_calls.append(request)
        response = MagicMock()
        response.__enter__ = MagicMock(return_value=response)
        response.__exit__ = MagicMock(return_value=False)
        response.status = 200
        return response

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("AGENTTEAMS_STATUS_WEBHOOK_URL", "http://env-sink:9099/status")
    _write_webhook_config(
        monkeypatch,
        tmp_path,
        {"AGENTTEAMS_STATUS_WEBHOOK_URL": "http://file-sink:9099/status"},
    )
    with caplog.at_level(logging.INFO, logger=status_report.logger.name):
        await report_tool_event(
            "taskflow",
            "ack_task",
            payload={"taskId": "t-1"},
            result=_tool_response({"ok": True, "task": {"task_id": "t-1", "status": "in_progress"}}),
        )
    assert [r.full_url for r in request_calls] == ["http://env-sink:9099/status"]


@pytest.mark.asyncio
async def test_webhook_bad_config_file_degrades_to_log_only(monkeypatch, tmp_path, caplog):
    request_calls: list[urllib.request.Request] = []

    def fake_urlopen(request, timeout=None):
        request_calls.append(request)
        response = MagicMock()
        response.__enter__ = MagicMock(return_value=response)
        response.__exit__ = MagicMock(return_value=False)
        response.status = 200
        return response

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    path = _write_webhook_config(
        monkeypatch, tmp_path, {"AGENTTEAMS_STATUS_WEBHOOK_URL": "http://file-sink:9099/status"}
    )
    path.write_text("{not-json", encoding="utf-8")
    with caplog.at_level(logging.INFO, logger=status_report.logger.name):
        # Must not raise and must still log the event.
        await report_tool_event(
            "projectflow",
            "create_project",
            payload={"projectId": "p-1"},
            result=_tool_response({"ok": True, "project": {"projectId": "p-1"}}),
        )
    assert request_calls == []
    assert len(_logged_events(caplog)) == 1
