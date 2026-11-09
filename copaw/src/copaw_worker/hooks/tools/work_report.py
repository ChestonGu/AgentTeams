"""Direct-collaboration work reporting tool (STATUS_EVENT direct_work_*).

Registered only when the team's runtime.yaml sets
``team.collaboration: direct`` — in that mode taskflow/projectflow are not
registered, so this tool is the agent's only status-reporting channel. The
agent calls it when real work starts (optional, recommended for long work)
and when it completes (required), alongside the Team Room reply.

Events reuse the exact delivery path of taskflow/projectflow reporting:
one INFO ``STATUS_EVENT`` line always, then the per-key env >
shared/config/webhook.json webhook with the 3-second at-most-once POST.
``shared/config/`` is pulled best-effort before each call (same contract as
the other protocol tools) so a webhook.json dropped later takes effect
without a restart.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from typing import Any

from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

from copaw_worker.hooks.tools.filesync import pull_shared_config
from copaw_worker.hooks.tools.status_report import (
    _current_actor,
    _runtime_team_name,
    emit_event,
)

logger = logging.getLogger(__name__)

_ACTIONS = ("started", "completed")
_RESULT_STATUSES = ("SUCCESS", "SUCCESS_WITH_NOTES", "BLOCKED")


def _response(payload: dict[str, Any]) -> ToolResponse:
    return ToolResponse(
        content=[
            TextBlock(
                type="text",
                text=json.dumps(payload, ensure_ascii=False),
            ),
        ],
    )


async def work_report(
    action: str,
    summary: str,
    deliverables: list[str] | None = None,
    requester: str | None = None,
    status: str | None = None,
) -> ToolResponse:
    """Report direct-collaboration work progress as a STATUS_EVENT.

    action: "started" when you begin real work (optional; recommended for
    multi-step or long work), "completed" when the work is done (required).
    summary: one short sentence describing the work or its outcome.
    deliverables: completed only — shared-relative result paths, e.g.
    ["shared/outputs/<worker>/report.md"].
    requester: Matrix ID of the member who @mentioned you, e.g.
    "@alice:example.com".
    status: completed only — SUCCESS (default), SUCCESS_WITH_NOTES, BLOCKED.
    """
    action = (action or "").strip().lower()
    if action not in _ACTIONS:
        return _response({
            "ok": False,
            "error": f"action must be one of {_ACTIONS}, got {action!r}",
        })
    summary = (summary or "").strip()
    if not summary:
        return _response({"ok": False, "error": "summary is required"})

    work: dict[str, Any] = {"summary": summary}
    requester = (requester or "").strip()
    if requester:
        work["requester"] = requester
    if action == "completed":
        result_status = (status or "SUCCESS").strip().upper()
        if result_status not in _RESULT_STATUSES:
            return _response({
                "ok": False,
                "error": f"status must be one of {_RESULT_STATUSES}, got {status!r}",
            })
        work["status"] = result_status
        paths = [str(p).strip() for p in (deliverables or []) if str(p).strip()]
        if paths:
            work["deliverables"] = paths

    # Refresh the team-shared webhook config before emitting (best-effort,
    # same contract as taskflow/projectflow calls).
    pull_shared_config()

    event: dict[str, Any] = {
        "event": f"direct_work_{action}",
        "team": _runtime_team_name(),
        "actor": _current_actor(),
        "work": work,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    event = {
        key: value
        for key, value in event.items()
        if value is not None and value != {} and value != []
    }
    await emit_event(event)
    return _response({"ok": True, "event": event["event"], "work": work})
