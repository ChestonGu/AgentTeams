#!/usr/bin/env python3
"""status_report — STATUS_EVENT envelopes for the protocol CLIs (bridge line).

CLI mirror of copaw's ``copaw_worker/hooks/tools/status_report.py``: after a
successful state-changing command the CLI emits one structured JSON event so
an external observer (CI dashboard, third-party system) can track project
and task progress without reading Matrix rooms or shared storage. The
envelope is field-compatible with the copaw line — both runtimes feed one
event stream:

    {"event": "taskflow.ack_task", "team": "t-1", "actor": "@w:dom",
     "projectId": "p-1", "taskId": "t-1", "taskStatus": "in_progress",
     "projectStatus": "active",
     "project": {...projects/<id>/meta.json...},
     "task": {...tasks/<id>/meta.json...},
     "tasks"/"readyNodes" (plan-dag), "loop" (plan-loop / record-iteration),
     "result" (submit / check), "projectTasks": [current status table],
     "timestamp": "..."}

CLI command -> envelope event mapping (actions stay copaw-named so the
two lines are indistinguishable to a consumer):

    taskflow ack / submit / check        -> taskflow.ack_task / submit_task / check_task
    projectflow create-project / plan-dag / plan-loop / record-iteration
                                         -> projectflow.* (snake_case names)
    projectflow pause/resume/complete    -> projectflow.pause_project / ...
    projectflow delegate-commit          -> taskflow.delegate_task (the step
                                            that marks the task assigned)
    projectflow check                    -> taskflow.check_task (leader read)

Delivery:

- one INFO line ``STATUS_EVENT <json>`` through the agentteams_log root
  logger — both sinks carry it (stderr for `kubectl logs` / the bridge's
  process capture, $AGENTTEAMS_FS_ROOT/logs/agentteams.log for post-mortem;
  AGENTTEAMS_LOG_STDERR=0 silences stderr as usual);
- optional webhook: $AGENTTEAMS_STATUS_WEBHOOK_URL POSTs the same envelope
  (optional bearer $AGENTTEAMS_STATUS_WEBHOOK_TOKEN; 3s timeout, failures
  logged and swallowed) — in the stateless form the sandbox stderr is not
  visible to the cluster, so the webhook is the realtime outbound channel;
- reporting never changes CLI behaviour: report_event catches its own
  errors and never raises into the command.

Context fields resolve from the AGENTTEAMS_* env contract (sandbox env
injection or /opt/teams/config/runtime.json, which mc_sync loads into
os.environ at import time): AGENTTEAMS_TEAM (team),
AGENTTEAMS_MATRIX_USER_ID (actor; the taskflow CLI's --actor flag wins).

Vendored copy — keep the four instances in sync: cli/taskflow,
cli/projectflow, template/worker-bridge-agent/skills/task-management/scripts
(worker sandbox images), template/worker-bridge-leader-agent/skills/
task-management/scripts (leader).
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
from typing import Any
import urllib.request

logger = logging.getLogger("status_report")

_WEBHOOK_URL_ENV = "AGENTTEAMS_STATUS_WEBHOOK_URL"
_WEBHOOK_TOKEN_ENV = "AGENTTEAMS_STATUS_WEBHOOK_TOKEN"
_WEBHOOK_TIMEOUT_SECONDS = 3


def _as_dict(value: Any) -> dict[str, Any] | None:
    """dataclass -> asdict, dict -> as-is, anything else -> None."""
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return value if isinstance(value, dict) else None


def _as_dicts(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, (list, tuple)):
        return None
    items = [_as_dict(item) for item in value]
    return items if all(item is not None for item in items) else None


def report_event(
    tool: str,
    action: str,
    root: Path | str,
    *,
    task: Any = None,
    project: Any = None,
    tasks: Any = None,
    ready_nodes: Any = None,
    loop: Any = None,
    result: Any = None,
    actor: str | None = None,
) -> None:
    """Emit one STATUS_EVENT after a successful state change. Never raises."""
    try:
        event = _build_event(
            tool, action, root, task=task, project=project, tasks=tasks,
            ready_nodes=ready_nodes, loop=loop, result=result, actor=actor,
        )
        logger.info("STATUS_EVENT %s", json.dumps(event, ensure_ascii=False))
        url = (os.getenv(_WEBHOOK_URL_ENV) or "").strip()
        if url:
            _post_webhook(url, event)
    except Exception:
        logger.exception("status report failed for %s.%s", tool, action)


def _build_event(
    tool: str,
    action: str,
    root: Path | str,
    *,
    task: Any,
    project: Any,
    tasks: Any,
    ready_nodes: Any,
    loop: Any,
    result: Any,
    actor: str | None,
) -> dict[str, Any]:
    task = _as_dict(task) or {}
    project = _as_dict(project) or {}
    project_id = project.get("project_id") or task.get("project_id")
    shared = Path(root) / "shared"
    event: dict[str, Any] = {
        "event": f"{tool}.{action}",
        "team": (os.getenv("AGENTTEAMS_TEAM") or "").strip() or None,
        "actor": (actor or os.getenv("AGENTTEAMS_MATRIX_USER_ID") or "").strip() or None,
        "projectId": project_id,
        "taskId": task.get("task_id"),
        "taskStatus": task.get("status"),
        "projectStatus": project.get("status")
        or _store_project_status(shared, project_id),
        # Full store metadata blocks — the same content that lands in
        # shared/projects/<id>/meta.json and shared/tasks/<id>/meta.json.
        "project": project or _read_project_meta(shared, project_id),
        "task": task,
        "tasks": _as_dicts(tasks),
        "readyNodes": _as_dicts(ready_nodes),
        "loop": _as_dict(loop),
        "result": _as_dict(result),
        "projectTasks": _snapshot_project_tasks(shared, project_id),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    # Drop empty blocks so read-only or project-only events stay compact.
    return {
        key: value
        for key, value in event.items()
        if value is not None and value != {} and value != []
    }


def _read_project_meta(shared: Path, project_id: str | None) -> dict[str, Any] | None:
    """Read shared/projects/<id>/meta.json locally (mirrors the MinIO copy)."""
    if not project_id:
        return None
    try:
        path = shared / "projects" / project_id / "meta.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
    except Exception:
        logger.debug("Unable to read project meta for %s", project_id, exc_info=True)
    return None


def _store_project_status(shared: Path, project_id: str | None) -> str | None:
    meta = _read_project_meta(shared, project_id)
    return meta.get("status") if meta else None


def _snapshot_project_tasks(
    shared: Path, project_id: str | None
) -> list[dict[str, Any]] | None:
    """Current status table of every delegated task in the project.

    Reads shared/tasks/*/meta.json from the local store (the same files the
    command just pushed to object storage). On the leader this is the
    complete table; on a worker sandbox it may only cover tasks that pod
    has pulled — empty results are omitted rather than reported as "no
    tasks".
    """
    if not project_id:
        return None
    rows: list[dict[str, Any]] = []
    try:
        tasks_dir = shared / "tasks"
        if not tasks_dir.is_dir():
            return None
        for meta_path in sorted(tasks_dir.glob("*/meta.json")):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(meta, dict) or meta.get("project_id") != project_id:
                continue
            rows.append(
                {
                    "taskId": meta.get("task_id"),
                    "title": meta.get("task_title"),
                    "assignedTo": meta.get("assigned_to"),
                    "status": meta.get("status"),
                    "dependsOn": meta.get("depends_on") or [],
                }
            )
    except Exception:
        logger.debug("Unable to snapshot project tasks for %s", project_id, exc_info=True)
        return None
    return rows or None


def _post_webhook(url: str, event: dict[str, Any]) -> None:
    body = json.dumps(event, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    token = os.getenv(_WEBHOOK_TOKEN_ENV, "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(
            request, timeout=_WEBHOOK_TIMEOUT_SECONDS
        ) as response:
            logger.debug("status webhook responded %s", response.status)
    except Exception as exc:
        logger.warning("status webhook delivery failed: %s", exc)
