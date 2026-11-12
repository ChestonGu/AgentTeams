#!/usr/bin/env python3
"""project_register — independent project-watch register for the bridge CLI.

After ``plan-dag`` succeeds, optionally POST the same event envelope the
status webhook sees to a dedicated register URL so an external service
can start watching the project's execution.

Delivery rules (mirrors status reporting semantics, but as a fully
independent module — status_report.py is untouched):

- Taskflow-only: AGENTTEAMS_COLLABORATION=direct skips the call.
- Fires only on a successful ``plan-dag`` (the caller checks the exit
  code; this module is invoked only from the success path).
- Target resolves per key, per call: explicit env first, then the
  team-shared <root>/shared/config/watchdog-register.json (flat JSON,
  {"AGENTTEAMS_WATCHDOG_REGISTER_URL": "<url>"}; a dedicated config file
  separate from the status webhook.json).
- 3-second timeout, at-most-once: non-2xx / timeout / connection failure
  is logged as a WARNING and the CLI exits normally.
- Never raises into the command.

Pure helper (_build_event) is imported from status_report — a read-only
function shared by both delivery paths; the HTTP call, config file, and
failure handling here are fully separate.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any
import urllib.request

from status_report import _build_event

logger = logging.getLogger("project_register")

_REGISTER_URL_ENV = "AGENTTEAMS_WATCHDOG_REGISTER_URL"
_REGISTER_TIMEOUT_SECONDS = 3


def _collaboration_mode() -> str:
    """Env-injected collaboration mode ("" = taskflow default)."""
    return (os.getenv("AGENTTEAMS_COLLABORATION") or "").strip().lower()


def _register_file_config(root: Path | str) -> dict[str, str]:
    """Team-shared register config: <root>/shared/config/watchdog-register.json.

    Flat JSON keyed by env name. Missing/unreadable/non-object degrades
    to an empty mapping (register disabled).
    """
    try:
        path = Path(root) / "shared" / "config" / "watchdog-register.json"
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def _post_register(url: str, event: dict[str, Any]) -> None:
    """POST one register payload. 3s timeout, WARN on failure. Never raises."""
    body = json.dumps(event, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(
            request, timeout=_REGISTER_TIMEOUT_SECONDS
        ) as response:
            logger.debug("project register responded %s", response.status)
    except Exception as exc:
        logger.warning("project register delivery failed: %s", exc)


def register_project_watch(
    tool: str,
    action: str,
    root: Path | str,
    *,
    project: Any = None,
    tasks: Any = None,
    ready_nodes: Any = None,
    actor: str | None = None,
) -> None:
    """After plan_dag succeeds, POST the event envelope to the register URL.

    The request body is the plan_dag STATUS_EVENT envelope (same payload
    the status webhook consumer sees). Independent from report_event —
    separate module, separate config file, separate call site. Never
    raises, never changes the CLI exit code.
    """
    try:
        if action != "plan_dag":
            return
        if _collaboration_mode() == "direct":
            return
        config = _register_file_config(root)
        url = ((os.getenv(_REGISTER_URL_ENV) or "").strip()
               or config.get(_REGISTER_URL_ENV, "").strip())
        if not url:
            return
        event = _build_event(
            tool, action, root,
            task=None, project=project, tasks=tasks,
            ready_nodes=ready_nodes, loop=None, result=None, actor=actor,
        )
        _post_register(url, event)
    except Exception:
        logger.warning("project register failed for plan_dag", exc_info=True)
