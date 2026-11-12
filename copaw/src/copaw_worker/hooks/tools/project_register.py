"""Project-watch register — independent from STATUS_EVENT delivery.

After ``plan_dag`` succeeds, optionally POST the same event envelope the
status webhook sees to a dedicated register URL so an external service
can start watching the project's execution.

Delivery rules (mirrors status reporting semantics, but as a fully
independent module — status_report.py is untouched):

- Taskflow-only: direct-collaboration teams skip the call entirely.
- Fires only on a successful, non-dry-run ``plan_dag``.
- Target resolves per key, per call: explicit env first, then the
  team-shared ``shared/config/watchdog-register.json`` (flat JSON,
  ``{"AGENTTEAMS_WATCHDOG_REGISTER_URL": "<url>"}``; a dedicated config
  file separate from the status webhook.json).
- 3-second timeout, at-most-once: non-2xx / timeout / connection failure
  is logged as a WARNING and the project continues normally.
- Never raises into the tool call.

Pure helpers (``_build_event`` etc.) are imported from status_report —
they are read-only functions shared by both delivery paths; the HTTP
call, config resolution, collaboration check, and failure handling here
are fully separate from ``emit_event``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.request
from typing import Any

from copaw_worker.hooks.tools.status_report import (
    _build_event,
    _coerce_payload_dict,
    _response_payload,
    _working_dir,
)

logger = logging.getLogger(__name__)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    logger.addHandler(_handler)
logger.setLevel(logging.INFO)

_REGISTER_URL_ENV = "AGENTTEAMS_WATCHDOG_REGISTER_URL"
_REGISTER_TIMEOUT_SECONDS = 3


def _runtime_collaboration() -> str:
    """Read team.collaboration from runtime.yaml ("" = taskflow default)."""
    try:
        import yaml

        path = _working_dir().parent / "runtime" / "runtime.yaml"
        if not path.exists():
            return ""
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        team = data.get("team") if isinstance(data, dict) else None
        if isinstance(team, dict):
            return str(team.get("collaboration") or "").strip().lower()
    except Exception:
        logger.debug("Unable to read collaboration from runtime.yaml", exc_info=True)
    return ""


def _register_file_config() -> dict[str, str]:
    """Team-shared register config: shared/config/watchdog-register.json.

    Flat JSON keyed by env name. Missing/unreadable/non-object degrades
    to an empty mapping (register disabled).
    """
    try:
        path = _working_dir() / "workspaces" / "default" / "shared" / "config" / "watchdog-register.json"
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        logger.debug("watchdog-register.json unreadable", exc_info=True)
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


async def register_project_watch(
    tool: str,
    action: str,
    payload: Any = None,
    result: Any = None,
) -> None:
    """After plan_dag succeeds, POST the event envelope to the register URL.

    The request body is the plan_dag STATUS_EVENT envelope (same payload
    the status webhook consumer sees, including the full DAG tasks and
    ready nodes). Independent from emit_event — separate module, separate
    config file, separate call site. Never raises, never blocks the tool.
    """
    try:
        if action != "plan_dag":
            return
        data = _response_payload(result) if result is not None else {}
        if data.get("dryRun") or not data.get("ok"):
            return
        if _runtime_collaboration() == "direct":
            return
        config = _register_file_config()
        url = (os.getenv(_REGISTER_URL_ENV, "").strip()
               or config.get(_REGISTER_URL_ENV, "").strip())
        if not url:
            return
        event = _build_event(tool, action, _coerce_payload_dict(payload), data)
        await asyncio.to_thread(_post_register, url, event)
        logger.debug("project register posted for %s", event.get("projectId"))
    except Exception:
        logger.warning("project register failed for plan_dag", exc_info=True)
