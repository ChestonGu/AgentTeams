#!/usr/bin/env python3
"""taskflow CLI — the opencode worker's task protocol commands.

opencode-worker-migration / M1 (T1 skeleton + T2 guardrails & sync).

Maps the copaw worker's ``taskflow`` tool calls onto a bash-invokable CLI:

    taskflow(action="check_task")   ->  taskflow check  <task-id>
    taskflow(action="ack_task")     ->  taskflow ack    <task-id>
    taskflow(action="submit_task")  ->  taskflow submit <task-id> --status ... --summary ...

Semantics follow copaw (docs/opencode-worker运行时迁移方案.md D0/D7):
  ack    one step: pull task dir -> verify identity -> assigned->in_progress
         -> push (excluding spec.md/base/, which are Leader-owned); the
         response PRINTS THE SPEC CONTENT (like copaw ack_task).
  submit NO pull first (copaw submit_task is purely local-transition-then-
         push): write protocol result.md -> in_progress->submitted -> push
         (same excludes) -> remote verify via mc stat.
  check  pull task dir, then read-only meta/status summary (spec
         intentionally NOT printed here; copaw never feeds spec through check).

Push/verify failures roll the local meta.json back to its pre-command state
(copaw has no rollback — this is the documented §1.3 enhancement).

Guardrails beyond the vendored core (kept in this CLI layer so the core stays
line-equivalent to copaw):
  - ack requires status in {assigned, in_progress}; in_progress is an
    idempotent success (re-ack prints the spec again, does not reset state).
  - submit requires status == in_progress (assigned -> "ack first";
    submitted -> duplicate rejected).
  - deliverable paths not starting with ``shared/`` are auto-prefixed with
    ``shared/tasks/<id>/`` before protocol validation.

Environment (contract §4.1):
  AGENTTEAMS_MATRIX_USER_ID  worker identity for the ownership guard (required)
  AGENTTEAMS_FS_ROOT         workspace root containing shared/ (default /root/agentteams-fs)

Exit codes: 0 ok, 1 taskflow error, 2 usage error.
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agentteams_log  # noqa: E402
from mc_sync import McSyncError  # noqa: E402
from taskflow_core import (  # noqa: E402
    RESULT_STATUSES,
    FileSystemTaskStore,
    TaskResult,
    TaskflowError,
    ack_task,
    check_task,
    is_effective_result,
    render_task_result,
    submit_task,
)

DEFAULT_FS_ROOT = "/root/agentteams-fs"


# ---------------------------------------------------------------------------
# Sync backend (T1: local-only; T2 adds the mc/MinIO backend)
# ---------------------------------------------------------------------------

class SyncBackend:
    """Keeps shared/tasks/<id>/ in step with MinIO inside task commands."""

    def pull_task(self, task_id: str) -> None:  # noqa: D102 - no-op base
        pass

    def push_task(self, task_id: str, exclude: list[str] | None = None) -> None:  # noqa: D102
        pass

    def verify_result(self, task_id: str) -> None:  # noqa: D102
        pass

    def describe(self) -> str:
        return "local-only"


class NoSyncBackend(SyncBackend):
    """T1 mode: the local shared/ tree is authoritative (smoke tests, unit tests)."""


# What ack/submit push with: Leader-owned files a worker must never overwrite.
TASK_PUSH_EXCLUDES = ["spec.md", "base/"]

# Set by main() so command functions can log without threading a logger.
LOG = None


def _log(event, level=None, **fields):
    if LOG is not None:
        agentteams_log.log_event(LOG, event, **({"level": level} if level else {}), **fields)


def make_sync_backend(name: str, root: Path) -> SyncBackend:
    if name == "none":
        return NoSyncBackend()
    if name == "mc":
        from mc_sync import McSyncBackend

        return McSyncBackend(root)
    raise TaskflowError(f"unknown sync backend: {name}")


def _push_with_rollback(
    store: FileSystemTaskStore,
    sync: SyncBackend,
    task_id: str,
    meta_before,
    *,
    verify: bool,
) -> None:
    """Push (and optionally verify) a task, rolling back meta.json on failure.

    Restores the pre-command meta.json so a failed network round-trip leaves
    the local state machine where it started and the command can be retried.
    A result.md written before the push stays on disk; it is simply rewritten
    by the retried submit. (copaw has no rollback — §1.3 enhancement.)
    """
    try:
        _log("push", task=task_id, exclude=TASK_PUSH_EXCLUDES,
             backend=sync.describe())
        sync.push_task(task_id, exclude=TASK_PUSH_EXCLUDES)
        if verify:
            _log("verify", task=task_id, target="result.md")
            sync.verify_result(task_id)
    except Exception as exc:
        store.write_task_meta(meta_before)
        _log("rollback", level=logging.WARNING, task=task_id,
             restored_status=meta_before.status, error=str(exc))
        raise TaskflowError(
            f"sync failed for task {task_id} ({exc}); "
            f"local status rolled back to '{meta_before.status}'"
        ) from exc


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_check(store: FileSystemTaskStore, args: argparse.Namespace, sync: SyncBackend) -> int:
    _log("pull", task=args.task_id, backend=sync.describe())
    sync.pull_task(args.task_id)
    meta = store.read_task_meta(args.task_id)
    result_line = "<none>"
    effective = "-"
    try:
        result = check_task(store, task_id=args.task_id)
        result_line = f"{result.status} — {result.summary}"
        effective = "yes" if is_effective_result(result) else "no"
    except TaskflowError:
        pass
    _log("check_read", task=args.task_id, status=meta.status,
         result=result_line.split(" — ")[0])
    print(f"task: {meta.task_id}")
    print(f"title: {meta.task_title}")
    print(f"status: {meta.status}")
    print(f"assigned_to: {meta.assigned_to}")
    print(f"room_id: {meta.room_id or '-'}")
    print(f"assigned_at: {meta.assigned_at or '-'}")
    print(f"acknowledged_at: {meta.acknowledged_at or '-'}")
    print(f"submitted_at: {meta.submitted_at or '-'}")
    print(f"result: {result_line}")
    if effective != "-":
        print(f"effective_for_acceptance: {effective}")
    return 0


def cmd_ack(store: FileSystemTaskStore, args: argparse.Namespace, sync: SyncBackend) -> int:
    _log("pull", task=args.task_id, backend=sync.describe())
    sync.pull_task(args.task_id)
    meta_before = store.read_task_meta(args.task_id)
    meta = meta_before
    if meta.status == "in_progress":
        # Idempotent re-ack: keep state, still honour the spec-in-response rule.
        _log("ack_idempotent", task=args.task_id)
        print(f"[ack] {meta.task_id}: already in_progress "
              f"(acknowledged_at={meta.acknowledged_at or '-'})")
    elif meta.status != "assigned":
        raise TaskflowError(
            f"cannot ack task {meta.task_id} in status '{meta.status}' "
            f"(expected 'assigned')"
        )
    else:
        meta = ack_task(store, task_id=args.task_id, actor=args.actor)
        _log("status_change", task=args.task_id, from_="assigned",
             to="in_progress")
        _push_with_rollback(store, sync, args.task_id, meta_before, verify=False)
        print(f"[ack] {meta.task_id}: assigned -> in_progress")
    print(f"[meta] title={meta.task_title} assigned_to={meta.assigned_to} "
          f"room={meta.room_id or '-'}")
    spec = store.read_task_spec(args.task_id)
    print(f"--- spec: shared/tasks/{meta.task_id}/spec.md ---")
    print(spec.rstrip())
    print("--- end of spec ---")
    return 0


def cmd_submit(store: FileSystemTaskStore, args: argparse.Namespace, sync: SyncBackend) -> int:
    # NOTE: no pull here — copaw's submit_task never pulls; it transitions
    # locally (result.md + status), pushes, and verifies remotely.
    meta_before = store.read_task_meta(args.task_id)
    if meta_before.status == "assigned":
        raise TaskflowError(f"task {args.task_id} not acknowledged yet; run 'taskflow ack' first")
    if meta_before.status != "in_progress":
        raise TaskflowError(
            f"cannot submit task {args.task_id} in status '{meta_before.status}'"
        )

    status = args.status.upper()
    if status not in RESULT_STATUSES:
        raise TaskflowError(
            f"invalid result status: {status} (choose from {', '.join(sorted(RESULT_STATUSES))})"
        )
    prefix = f"shared/tasks/{args.task_id}/"
    deliverables = [
        item if item.startswith("shared/") else prefix + item
        for item in args.deliverables
    ]
    result = TaskResult(status=status, summary=args.summary,
                        deliverables=deliverables, notes=list(args.notes or []))

    meta = submit_task(store, task_id=args.task_id, result=result, actor=args.actor)
    _log("status_change", task=args.task_id, from_="in_progress",
         to="submitted", result_status=status,
         deliverables=len(deliverables))
    _push_with_rollback(store, sync, args.task_id, meta_before, verify=True)
    print(f"[submit] {meta.task_id}: in_progress -> submitted ({status})")
    print("[result.md]")
    print(render_task_result(result).rstrip())
    print(f"[sync] {sync.describe()}")
    return 0


# ---------------------------------------------------------------------------
# CLI plumbing
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="taskflow",
        description="AgentTeams worker task protocol (copaw-compatible)",
    )
    parser.add_argument(
        "--root",
        default=os.environ.get("AGENTTEAMS_FS_ROOT", DEFAULT_FS_ROOT),
        help=f"workspace root containing shared/ (default: {DEFAULT_FS_ROOT} or $AGENTTEAMS_FS_ROOT)",
    )
    parser.add_argument(
        "--actor",
        default=os.environ.get("AGENTTEAMS_MATRIX_USER_ID"),
        help="worker identity for the ownership guard (default: $AGENTTEAMS_MATRIX_USER_ID)",
    )
    parser.add_argument(
        "--sync",
        choices=["none", "mc"],
        default="mc",
        help="sync backend: mc = MinIO via the copaw filesync env contract "
             "(default, the deployed form — copaw's taskflow always syncs); "
             "none = local shared/ only (tests)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="show task meta/status summary (read-only)")
    p_check.add_argument("task_id")

    p_ack = sub.add_parser("ack", help="accept a task; response includes the spec content")
    p_ack.add_argument("task_id")

    p_submit = sub.add_parser("submit", help="submit a structured task result")
    p_submit.add_argument("task_id")
    p_submit.add_argument(
        "--status", required=True,
        help=f"one of {', '.join(sorted(RESULT_STATUSES))}",
    )
    p_submit.add_argument("--summary", required=True, help="one-paragraph result summary")
    p_submit.add_argument(
        "--deliverables", nargs="*", default=[],
        help="deliverable paths; bare paths are auto-prefixed shared/tasks/<id>/",
    )
    p_submit.add_argument("--notes", nargs="*", default=[], help="optional note lines")

    return parser


def main(argv: list[str] | None = None) -> int:
    global LOG
    started = time.monotonic()
    args = build_parser().parse_args(argv)
    LOG = agentteams_log.setup("taskflow",
                               argv if argv is not None else sys.argv[1:])
    _log("context", root=args.root, sync=args.sync,
         actor=agentteams_log.clip(args.actor or "-"))
    store = FileSystemTaskStore(args.root)
    code = 1
    try:
        if not args.actor:
            raise TaskflowError(
                "worker identity missing: set AGENTTEAMS_MATRIX_USER_ID or pass --actor"
            )
        sync = make_sync_backend(args.sync, Path(args.root))
        if args.command == "check":
            code = cmd_check(store, args, sync)
        elif args.command == "ack":
            code = cmd_ack(store, args, sync)
        elif args.command == "submit":
            code = cmd_submit(store, args, sync)
        else:
            raise TaskflowError(f"unknown command: {args.command}")
    except (TaskflowError, McSyncError) as exc:
        _log("error", level=logging.ERROR, error=str(exc))
        print(f"[taskflow] error: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr or exc.stdout or ""
        _log("error", level=logging.ERROR, error="mc command failed",
             mc_returncode=exc.returncode)
        print(f"[taskflow] mc command failed ({exc.returncode}): "
              f"{detail.strip()[:500]}", file=sys.stderr)
        return 1
    except (OSError, RuntimeError) as exc:
        _log("error", level=logging.ERROR, error=str(exc))
        print(f"[taskflow] sync error: {exc}", file=sys.stderr)
        return 1
    finally:
        agentteams_log.log_cmd_end(LOG, code if code == 0 else 1, started,
                                   command=args.command,
                                   task=getattr(args, "task_id", None))
    return code


if __name__ == "__main__":
    sys.exit(main())
