#!/usr/bin/env python3
"""agentteams-sync CLI — the opencode worker's shared-file sync commands.

opencode-worker-migration / M1 (T3). Maps the copaw worker's ``filesync``
tool (hooks/tools/filesync.py) onto a bash-invokable CLI:

    filesync(action="pull", path=...)                ->  agentteams-sync pull <path>
    filesync(action="push", path=..., exclude=[...]) ->  agentteams-sync push <path> [--exclude p ...]
    filesync(action="stat", path=...)                ->  agentteams-sync stat <path>
    filesync(action="list", path=...)                ->  agentteams-sync list <path>

Semantics copied from copaw:
  - pull/push/list auto-append "/" for ``{shared,global-shared}/{projects,tasks}/<id>``
    (agents usually omit the trailing slash on task/project dirs); stat uses
    the path verbatim (exact object match).
  - stdout is the same JSON payload copaw handed the agent —
    ``{"ok": true, action, path, localPath, kind, ...}`` with ``pulled`` /
    ``pushed`` / ``exists`` / ``entries`` per action. Failures print
    ``{"ok": false, "error", action, path}`` and exit 1, so bash can branch
    on $? while the JSON stays parseable.
  - push: ``global-shared/`` is read-only; ``--exclude`` maps 1:1 to copaw's
    exclude list (glob patterns handed to ``mc mirror``).
  - ``--dry-run`` resolves the path and echoes the payload without touching
    storage (copaw's dryRun).

Env (§4.1, same contract as taskflow --sync mc):
  AGENTTEAMS_WORKER_NAME (required), AGENTTEAMS_FS_ENDPOINT / ACCESS_KEY /
  SECRET_KEY (required in local mode), AGENTTEAMS_FS_BUCKET (default
  agentteams-storage), AGENTTEAMS_WORKER_CR_NAME, AGENTTEAMS_RUNTIME;
  workspace root via --root or $AGENTTEAMS_FS_ROOT (default /root/agentteams-fs).

mc_sync.py must sit next to this script (flat deployment) or one directory
up under taskflow/ (dev tree) — it carries the vendored McFileSync engine
shared with the taskflow CLI.

Exit codes: 0 ok, 1 filesync error (JSON with ok:false on stdout).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time

# Locate the shared mc_sync + agentteams_log modules (vendored copaw
# FileSync subset + the unified JSONL logger).
_HERE = Path(__file__).resolve().parent
for _candidate in (_HERE, _HERE.parent / "taskflow", _HERE.parent.parent / "taskflow"):
    if (_candidate / "mc_sync.py").exists():
        sys.path.insert(0, str(_candidate))
        break

import agentteams_log  # noqa: E402
from mc_sync import filesync_from_env  # noqa: E402

DEFAULT_FS_ROOT = "/root/agentteams-fs"


def _normalize_directory_path(path: str) -> str:
    """Normalize common shared directory paths agents often omit '/' for.

    Verbatim from copaw hooks/tools/filesync.py.
    """
    stripped = path.strip()
    if stripped.endswith("/"):
        return stripped

    normalized = stripped.strip("/")
    parts = normalized.split("/")
    if (
        len(parts) == 3
        and parts[0] in {"shared", "global-shared"}
        and parts[1] in {"projects", "tasks"}
        and parts[2]
    ):
        return f"{normalized}/"

    return stripped


def _emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentteams-sync",
        description="AgentTeams worker shared-file sync (copaw filesync-compatible)",
    )
    parser.add_argument(
        "--root",
        default=os.environ.get("AGENTTEAMS_FS_ROOT", DEFAULT_FS_ROOT),
        help=f"workspace root containing shared/ (default: {DEFAULT_FS_ROOT} or $AGENTTEAMS_FS_ROOT)",
    )

    sub = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("path", help="shared/... or global-shared/... path")
    common.add_argument(
        "--dry-run", action="store_true",
        help="resolve and echo the payload without touching storage",
    )

    sub.add_parser("pull", parents=[common], help="pull a shared path from MinIO")
    p_push = sub.add_parser("push", parents=[common], help="push a local shared path to MinIO")
    p_push.add_argument(
        "--exclude", action="append", default=[],
        help="mc mirror glob pattern to skip; repeat for several (e.g. --exclude spec.md --exclude base/)",
    )
    sub.add_parser("stat", parents=[common], help="check a shared path exists in MinIO")
    sub.add_parser("list", parents=[common], help="list a shared path in MinIO")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log = agentteams_log.setup("agentteams-sync",
                               argv if argv is not None else sys.argv[1:])
    started = time.monotonic()
    code = 1

    try:
        fs = filesync_from_env(Path(args.root))
        path = (
            _normalize_directory_path(args.path)
            if args.command in {"pull", "push", "list"}
            else args.path.strip()
        )
        resolved = fs.resolve_shared_path(path)
        payload: dict = {
            "action": args.command,
            "path": path,
            "localPath": str(resolved.local),
            "kind": resolved.kind,
        }
        if args.command == "push":
            payload["exclude"] = list(args.exclude)

        if args.dry_run:
            agentteams_log.log_event(log, "action", dry_run=True,
                                     action=args.command, path=path,
                                     kind=resolved.kind)
            _emit({"ok": True, "dryRun": True, **payload})
            code = 0
        elif args.command == "pull":
            fs.pull_shared_path(path)
            _emit({"ok": True, "pulled": True, **payload})
            agentteams_log.log_event(log, "action", action=args.command,
                                     path=path, kind=resolved.kind)
            code = 0
        elif args.command == "push":
            fs.push_shared_path(path, exclude=list(args.exclude))
            _emit({"ok": True, "pushed": True, **payload})
            agentteams_log.log_event(log, "action", action=args.command,
                                     path=path, kind=resolved.kind,
                                     exclude=list(args.exclude) or None)
            code = 0
        elif args.command == "stat":
            fs.stat_shared_path(path)
            _emit({"ok": True, "exists": True, **payload})
            agentteams_log.log_event(log, "action", action=args.command,
                                     path=path, kind=resolved.kind)
            code = 0
        else:
            _, entries = fs.list_shared_path(path)
            _emit({"ok": True, "entries": entries, **payload})
            agentteams_log.log_event(log, "action", action=args.command,
                                     path=path, kind=resolved.kind,
                                     entries=len(entries))
            code = 0
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        message = f"mc {args.command} failed: {detail or exc}"
        agentteams_log.log_event(log, "error", level=logging.ERROR, action=args.command,
                                 path=args.path, error=agentteams_log.clip(message))
        _emit({"ok": False, "error": message, "action": args.command, "path": args.path})
    except Exception as exc:  # defensive boundary, same as copaw filesync
        agentteams_log.log_event(log, "error", level=logging.ERROR, action=args.command,
                                 path=args.path, error=str(exc))
        _emit({"ok": False, "error": str(exc), "action": args.command, "path": args.path})
    finally:
        agentteams_log.log_cmd_end(log, code, started, command=args.command)
    return code


if __name__ == "__main__":
    sys.exit(main())
