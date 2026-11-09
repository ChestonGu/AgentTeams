"""Fire-and-forget status reporting for projectflow/taskflow tool calls.

Every state-changing tool action emits one structured JSON event so an
external observer (CI dashboard, third-party system) can track project
and task progress without reading Matrix rooms or shared storage.

Delivery rules:

- Events are always logged as one INFO line prefixed ``STATUS_EVENT``
  followed by the JSON envelope, so the hook can be verified from pod
  logs alone.
- The envelope embeds the full store metadata for what changed — the
  same dicts that land in ``shared/projects/<id>/meta.json`` and
  ``shared/tasks/<id>/meta.json`` (and are synced to object storage):
  ``project`` (ProjectMeta), ``task`` (TaskMeta with timestamps),
  ``tasks``/``readyNodes`` (the planned DAG for plan_dag),
  ``loop`` (LoopPlan), ``result`` (TaskResult on submit/check), plus a
  ``projectTasks`` snapshot — the current status table of every
  delegated task in the project, read from the local store.
- When ``AGENTTEAMS_STATUS_WEBHOOK_URL`` is set, the envelope is also
  POSTed there as JSON (optional bearer token via
  ``AGENTTEAMS_STATUS_WEBHOOK_TOKEN``). Delivery waits at most a few
  seconds and every failure is logged and swallowed.
- The webhook target resolves per event, per key: explicit env first,
  then the team-shared ``shared/config/webhook.json`` in the local
  workspace copy (flat JSON, same key names; refreshed by the regular
  filesync pull). A missing or unreadable file degrades silently to
  log-only reporting.
- Reporting must never change tool behavior: ``report_tool_event``
  catches its own errors and never raises into the tool call.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sys
from typing import TYPE_CHECKING, Any

import urllib.request

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps module import-light
    from agentscope.tool import ToolResponse

logger = logging.getLogger(__name__)
# The CoPaw host process configures its own logging tree and leaves the root
# logger at WARNING, which would swallow these INFO events in pod logs. Attach
# a private stderr handler so STATUS_EVENT lines are always visible from
# `kubectl logs`, independent of any host-side logging configuration.
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    logger.addHandler(_handler)

# Actions that change project/task state and are worth reporting to an
# external observer. Read-only actions (ready_nodes, ready_loop_nodes,
# check_active_tasks) stay out to keep the event stream signal-dense.
REPORTED_ACTIONS: dict[str, set[str]] = {
    "projectflow": {
        "create_project",
        "plan_dag",
        "plan_loop",
        "record_loop_iteration",
        "pause_project",
        "resume_project",
        "complete_project",
    },
    "taskflow": {
        "delegate_task",
        "ack_task",
        "submit_task",
        "check_task",
    },
}

_WEBHOOK_URL_ENV = "AGENTTEAMS_STATUS_WEBHOOK_URL"
_WEBHOOK_TOKEN_ENV = "AGENTTEAMS_STATUS_WEBHOOK_TOKEN"
_WEBHOOK_TIMEOUT_SECONDS = 3


def _working_dir() -> Path:
    configured = os.getenv("QWENPAW_WORKING_DIR") or os.getenv("COPAW_WORKING_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    # qwenpaw is the successor of copaw (renamed package).
    try:
        from qwenpaw.constant import WORKING_DIR  # type: ignore[import-untyped]
    except ImportError:
        from copaw.constant import WORKING_DIR  # type: ignore[import-untyped]
    return Path(WORKING_DIR).expanduser().resolve()


def _current_actor() -> str | None:
    """Resolve the acting agent's Matrix user ID (env first, agent.json fallback)."""
    configured = os.getenv("AGENTTEAMS_MATRIX_USER_ID") or os.getenv("COPAW_MATRIX_USER_ID")
    if configured:
        return configured.strip()
    try:
        # Read agent.json directly (mirrors taskflow._current_actor) so the
        # event carries an actor even when the host process exports no env.
        agent_json = _working_dir() / "workspaces" / "default" / "agent.json"
        data = json.loads(agent_json.read_text(encoding="utf-8"))
        matrix_cfg = (data.get("channels") or {}).get("matrix") or {}
        user_id = matrix_cfg.get("user_id") or matrix_cfg.get("userId")
        if user_id:
            return str(user_id).strip()
    except Exception:
        logger.debug("Unable to resolve actor from agent.json", exc_info=True)
    return None


def _runtime_team_name() -> str | None:
    try:
        import yaml

        path = _working_dir().parent / "runtime" / "runtime.yaml"
        if not path.exists():
            return None
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        team = data.get("team") if isinstance(data, dict) else None
        if isinstance(team, dict):
            for key in ("name", "teamName", "teamId"):
                value = team.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    except Exception:
        logger.debug("Unable to read team name from runtime.yaml", exc_info=True)
    return None


def _response_payload(result: Any) -> dict[str, Any]:
    block = result.content[0]
    text = block.get("text") if isinstance(block, dict) else block.text
    data = json.loads(text)
    return data if isinstance(data, dict) else {}


def _coerce_payload_dict(payload: Any) -> dict[str, Any]:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return {}
    return payload if isinstance(payload, dict) else {}


def _build_event(
    tool: str,
    action: str,
    payload: dict[str, Any],
    data: dict[str, Any],
) -> dict[str, Any]:
    task = data.get("task") if isinstance(data.get("task"), dict) else {}
    project = data.get("project") if isinstance(data.get("project"), dict) else {}
    project_id = (
        project.get("projectId")
        or project.get("project_id")
        or task.get("projectId")
        or task.get("project_id")
        or payload.get("projectId")
    )
    event: dict[str, Any] = {
        "event": f"{tool}.{action}",
        "team": _runtime_team_name(),
        "actor": _current_actor(),
        "projectId": project_id,
        "taskId": task.get("taskId") or task.get("task_id") or payload.get("taskId"),
        "taskStatus": task.get("status"),
        "projectStatus": project.get("status") or _store_project_status(project_id),
        # Full store metadata blocks (snake_case, exactly what lands in
        # shared/projects/<id>/meta.json and shared/tasks/<id>/meta.json).
        "project": project or _read_project_meta(project_id),
        "task": task,
        "tasks": data.get("tasks") if isinstance(data.get("tasks"), list) else None,
        "readyNodes": (
            data.get("readyNodes") if isinstance(data.get("readyNodes"), list) else None
        ),
        "loop": data.get("loop") if isinstance(data.get("loop"), dict) else None,
        "result": data.get("result") if isinstance(data.get("result"), dict) else None,
        "projectTasks": _snapshot_project_tasks(project_id),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return {
        key: value
        for key, value in event.items()
        if value is not None and value != {} and value != []
    }


def _shared_dir() -> Path:
    return _working_dir() / "workspaces" / "default" / "shared"


def _read_project_meta(project_id: str | None) -> dict[str, Any] | None:
    """Read shared/projects/<id>/meta.json locally (mirrors the S3 copy)."""
    if not project_id:
        return None
    try:
        path = _shared_dir() / "projects" / project_id / "meta.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
    except Exception:
        logger.debug("Unable to read project meta for %s", project_id, exc_info=True)
    return None


def _store_project_status(project_id: str | None) -> str | None:
    meta = _read_project_meta(project_id)
    return meta.get("status") if meta else None


def _snapshot_project_tasks(project_id: str | None) -> list[dict[str, Any]] | None:
    """Current status table of every delegated task in the project.

    Reads shared/tasks/*/meta.json from the local store (the same files that
    are synced to object storage). On the leader this is the complete table;
    on a worker pod it may only cover tasks that pod has pulled — empty
    results are omitted rather than reported as "no tasks".
    """
    if not project_id:
        return None
    rows: list[dict[str, Any]] = []
    try:
        tasks_dir = _shared_dir() / "tasks"
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


def _post_webhook(url: str, event: dict[str, Any], token: str = "") -> None:
    body = json.dumps(event, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
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


def _webhook_file_config() -> dict[str, str]:
    """Team-shared webhook config fallback: shared/config/webhook.json.

    Flat JSON keyed by env names, e.g. {"AGENTTEAMS_STATUS_WEBHOOK_URL":
    "http://..."}; the local copy is refreshed by the regular filesync
    pull. Only string values apply. Any read/parse error or a non-object
    body degrades to an empty mapping (log-only reporting).
    """
    try:
        path = _shared_dir() / "config" / "webhook.json"
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


async def report_tool_event(
    tool: str,
    action: str,
    *,
    payload: Any = None,
    result: Any = None,
) -> None:
    """Emit one status event for a finished tool call. Never raises."""
    try:
        if action not in REPORTED_ACTIONS.get(tool, set()):
            return
        data = _response_payload(result) if result is not None else {}
        if data.get("dryRun") or not data.get("ok"):
            return
        event = _build_event(tool, action, _coerce_payload_dict(payload), data)
        await emit_event(event)
    except Exception:
        logger.exception("status report failed for %s.%s", tool, action)


async def emit_event(event: dict[str, Any]) -> None:
    """Log + optionally POST one pre-built event envelope. Never raises.

    Shared delivery path for taskflow/projectflow events (report_tool_event)
    and direct-collaboration work events (work_report): one INFO
    ``STATUS_EVENT`` line always, then the per-key env > shared/config/
    webhook.json webhook resolution with the 3-second at-most-once POST.
    """
    try:
        logger.info("STATUS_EVENT %s", json.dumps(event, ensure_ascii=False))
        config = _webhook_file_config()
        url = (os.getenv(_WEBHOOK_URL_ENV, "").strip()
               or config.get(_WEBHOOK_URL_ENV, "").strip())
        if url:
            token = (os.getenv(_WEBHOOK_TOKEN_ENV, "").strip()
                     or config.get(_WEBHOOK_TOKEN_ENV, "").strip())
            await asyncio.to_thread(_post_webhook, url, event, token)
    except Exception:
        logger.exception("status event emission failed")
