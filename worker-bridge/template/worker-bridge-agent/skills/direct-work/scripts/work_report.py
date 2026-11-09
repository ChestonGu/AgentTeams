#!/usr/bin/env python3
"""work-report — direct-collaboration STATUS_EVENT CLI (bridge line).

The direct-work skill's only status channel: in direct teams taskflow is
not shipped, so work progress is reported through this CLI. Emits
``direct_work_started`` / ``direct_work_completed`` envelopes through the
exact delivery path of the taskflow/projectflow CLIs (vendored
status_report.emit_event): one INFO ``STATUS_EVENT`` line always, then the
per-key env > <root>/shared/config/webhook.json webhook with the 3-second
at-most-once POST. shared/config/ is pulled best-effort before each run
(same contract as the other protocol CLIs).

Usage:
  work-report started   --summary "..." [--requester @user:domain]
  work-report completed --summary "..." [--deliverables shared/outputs/<w>/a.md ...]
                        [--requester @user:domain] [--status SUCCESS|SUCCESS_WITH_NOTES|BLOCKED]

Options:
  --root   workspace root containing shared/ (default $AGENTTEAMS_FS_ROOT
           or /root/agentteams-fs)
  --sync   mc (default; pull shared/config before reporting) | none
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mc_sync import McSyncBackend, McSyncError  # noqa: E402
from status_report import emit_event  # noqa: E402

# STATUS_EVENT lines must reach stderr without relying on the host CLI's
# logging setup (this script is invoked directly by the agent via work-report,
# not through taskflow's agentteams_log bootstrap).
_status_logger = logging.getLogger("status_report")
_status_logger.setLevel(logging.INFO)
if not _status_logger.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _status_logger.addHandler(_handler)
_status_logger.propagate = False

DEFAULT_FS_ROOT = "/root/agentteams-fs"
_ACTIONS = ("started", "completed")
_RESULT_STATUSES = ("SUCCESS", "SUCCESS_WITH_NOTES", "BLOCKED")


def build_event(args: argparse.Namespace) -> dict:
    work: dict = {"summary": args.summary.strip()}
    if args.requester:
        work["requester"] = args.requester.strip()
    if args.action == "completed":
        work["status"] = args.status
        paths = [p.strip() for p in (args.deliverables or []) if p.strip()]
        if paths:
            work["deliverables"] = paths
    event = {
        "event": f"direct_work_{args.action}",
        "team": (os.getenv("AGENTTEAMS_TEAM") or "").strip() or None,
        "actor": (os.getenv("AGENTTEAMS_MATRIX_USER_ID") or "").strip() or None,
        "work": work,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return {
        key: value
        for key, value in event.items()
        if value is not None and value != {} and value != []
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="work-report",
        description="Emit a direct-collaboration STATUS_EVENT (started/completed).")
    ap.add_argument("action", choices=_ACTIONS)
    ap.add_argument("--summary", required=True,
                    help="one short sentence describing the work or its outcome")
    ap.add_argument("--deliverables", nargs="*", default=None,
                    help="completed only: shared-relative result paths")
    ap.add_argument("--requester", default=None,
                    help="Matrix ID of the member who @mentioned you")
    ap.add_argument("--status", default="SUCCESS", choices=_RESULT_STATUSES,
                    help="completed only (default SUCCESS)")
    ap.add_argument("--root", default=os.environ.get("AGENTTEAMS_FS_ROOT", DEFAULT_FS_ROOT))
    ap.add_argument("--sync", default="mc", choices=("mc", "none"))
    args = ap.parse_args(argv)

    if not args.summary.strip():
        print("work-report: --summary must not be empty", file=sys.stderr)
        return 2

    if args.sync == "mc":
        try:
            McSyncBackend(Path(args.root)).pull_config()
        except (McSyncError, RuntimeError) as exc:
            # No sync environment (standalone sandbox) — log-only reporting.
            print(f"work-report: shared/config pull skipped: {exc}", file=sys.stderr)

    event = build_event(args)
    emit_event(event, args.root)
    print(json.dumps({"ok": True, "event": event["event"], "work": event["work"]},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
