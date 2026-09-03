#!/usr/bin/env python3
"""projectflow CLI — the opencode leader's project/task protocol commands.

opencode-worker-migration / leader side (prepares a future migration of
the Leader runtime to opencode; the copaw Leader stays authoritative
until then). Maps the copaw Leader's ``taskflow`` tool calls onto
bash-invokable commands over the SAME protocol files (plan.md, task
meta.json/spec.md/result.md), so a mixed copaw-leader + opencode-worker
team — or later an opencode leader + copaw workers — stays
byte-compatible:

    taskflow(action="create_project")   ->  projectflow create-project <id> --title ...
    taskflow(action="add_tasks")        ->  projectflow add-tasks <id> --tasks-json ...
    taskflow(action="plan_dag")         ->  projectflow plan-dag <id> --tasks-json ...
    taskflow(action="plan_loop")        ->  projectflow plan-loop <id> --goal ... --stop-condition ...
    taskflow(action="ready_nodes")      ->  projectflow ready <id>          (auto dag/loop)
    taskflow(action="record_loop_iteration") -> projectflow record-iteration <id> ...
    taskflow(action="prepare_task")     ->  projectflow delegate <pid> <tid> --spec ...
    taskflow(action="commit_task_assignment") -> projectflow delegate-commit <pid> <tid> [--event-id ...]
    taskflow(action="pause_project")    ->  projectflow pause-project <id>
    taskflow(action="resume_project")   ->  projectflow resume-project <id>
    taskflow(action="complete_project") ->  projectflow complete-project <id>
    taskflow(action="check_task")       ->  projectflow check <task-id>    (leader read)

Leader-side sync semantics (differs from the worker taskflow CLI):
  * the leader IS the protocol owner, so pushes carry NO excludes —
    spec.md / meta.json / plan.md flow from leader to storage;
  * every command pulls what it needs first (project dir, task dir),
    mutates, pushes back, and on sync failure restores the local files
    to their pre-command state (snapshot/rollback, same enhancement
    class as taskflow's, §5.3).

Structured payloads (--tasks-json, --spec, --iteration-template) accept
literal text, ``@path`` to read a file, or ``-`` for stdin.

Environment (contract §1):
  AGENTTEAMS_FS_ROOT  workspace root containing shared/ (default /root/agentteams-fs)

Exit codes: 0 ok, 1 projectflow error, 2 usage error.
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

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agentteams_log  # noqa: E402
from mc_sync import (  # noqa: E402
    McFileSync,
    McSyncError,
    _looks_like_missing_object_error,
    filesync_from_env,
)
from projectflow_core import (  # noqa: E402
    FileSystemTaskStore,
    TaskflowError,
    add_tasks,
    check_task,
    commit_task_assignment,
    complete_project,
    create_project,
    delegate_task,
    is_effective_result,
    parse_plan_type,
    pause_project,
    plan_dag,
    plan_loop,
    ready_loop_nodes,
    ready_nodes,
    record_loop_iteration,
    render_dag_task,
    render_task_result,
    resume_project,
)

DEFAULT_FS_ROOT = "/root/agentteams-fs"

# The leader owns every protocol file: nothing is excluded on push.
LEADER_PUSH_EXCLUDES: list[str] = []

# Set by main() so command functions can log without threading a logger.
LOG = None


def _log(event, level=None, **fields):
    if LOG is not None:
        agentteams_log.log_event(LOG, event, **({"level": level} if level else {}), **fields)


# ---------------------------------------------------------------------------
# Sync backend
# ---------------------------------------------------------------------------
class SyncBackend:
    """Keeps shared/projects/<id>/ and shared/tasks/<id>/ in step with MinIO."""

    def pull_project(self, project_id: str) -> None:  # noqa: D102 - no-op base
        pass

    def push_project(self, project_id: str) -> None:  # noqa: D102
        pass

    def pull_task(self, task_id: str, *, must_exist: bool = True) -> None:  # noqa: D102
        pass

    def push_task(self, task_id: str) -> None:  # noqa: D102
        pass

    def verify_result(self, task_id: str) -> None:  # noqa: D102
        pass

    def describe(self) -> str:
        return "local-only"


class NoSyncBackend(SyncBackend):
    """Local shared/ is authoritative (tests)."""


class McLeaderSyncBackend(SyncBackend):
    """Leader sync over McFileSync — pulls/pushes with NO excludes."""

    def __init__(self, root: Path | str, fs: McFileSync | None = None) -> None:
        self.root = Path(root)
        self._fs = fs if fs is not None else self._fs_from_env(self.root)

    @classmethod
    def _fs_from_env(cls, root: Path) -> McFileSync:
        return filesync_from_env(root)

    def _pull(self, path: str, what: str, must_exist: bool) -> None:
        try:
            self._fs.pull_shared_path(path)
        except subprocess.CalledProcessError as exc:
            detail = getattr(exc, "stderr", None) or getattr(exc, "stdout", None)
            if not must_exist and _looks_like_missing_object_error(detail):
                return  # first delegation of a brand-new task dir
            raise

    def pull_project(self, project_id: str) -> None:
        self._pull(f"shared/projects/{project_id}/", f"project {project_id}",
                   must_exist=True)

    def push_project(self, project_id: str) -> None:
        self._fs.push_shared_path(f"shared/projects/{project_id}/",
                                  exclude=LEADER_PUSH_EXCLUDES)

    def pull_task(self, task_id: str, *, must_exist: bool = True) -> None:
        self._pull(f"shared/tasks/{task_id}/", f"task {task_id}",
                   must_exist=must_exist)

    def push_task(self, task_id: str) -> None:
        self._fs.push_shared_path(f"shared/tasks/{task_id}/",
                                  exclude=LEADER_PUSH_EXCLUDES)

    def verify_result(self, task_id: str) -> None:
        self._fs.stat_shared_path(f"shared/tasks/{task_id}/result.md")

    def describe(self) -> str:
        try:
            remote = self._fs._get_shared_remote()
        except Exception:
            remote = "<unresolved>"
        return f"mc: bucket={self._fs.bucket} remote={remote}"


def make_sync_backend(name: str, root: Path) -> SyncBackend:
    if name == "none":
        return NoSyncBackend()
    if name == "mc":
        return McLeaderSyncBackend(root)
    raise TaskflowError(f"unknown sync backend: {name}")


# ---------------------------------------------------------------------------
# Snapshot/rollback (§5.3 enhancement class, leader form)
# ---------------------------------------------------------------------------
def _snapshot(paths: list[Path]) -> dict[Path, bytes | None]:
    return {p: p.read_bytes() if p.exists() else None for p in paths}


def _restore(store: FileSystemTaskStore, snap: dict[Path, bytes | None]) -> None:
    for path, data in snap.items():
        if data is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)


def _push_with_rollback(
    store: FileSystemTaskStore,
    sync: SyncBackend,
    snap: dict[Path, bytes | None],
    push,
) -> None:
    """Run the push callbacks; on failure restore the snapshotted files."""

    def targets():
        return sorted(str(p.relative_to(store.shared_dir)) for p in snap)

    try:
        _log("push", backend=sync.describe(), targets=targets())
        push()
    except Exception as exc:
        _restore(store, snap)
        _log("rollback", level=logging.WARNING, targets=targets(),
             error=str(exc))
        raise TaskflowError(
            f"sync failed ({exc}); local files rolled back to pre-command state"
        ) from exc


def _project_paths(store: FileSystemTaskStore, project_id: str) -> list[Path]:
    pdir = store._project_dir(project_id)
    return [pdir / "meta.json", pdir / "plan.md"]


def _task_paths(store: FileSystemTaskStore, task_id: str) -> list[Path]:
    tdir = store._task_dir(task_id)
    return [tdir / "meta.json", tdir / "spec.md", tdir / "result.md"]


# ---------------------------------------------------------------------------
# Payload helpers
# ---------------------------------------------------------------------------
def read_payload(value: str, argname: str) -> str:
    """Literal text, @path, or - for stdin."""
    if value == "-":
        return sys.stdin.read()
    if value.startswith("@"):
        path = Path(value[1:])
        if not path.exists():
            raise TaskflowError(f"{argname}: file not found: {path}")
        return path.read_text(encoding="utf-8")
    return value


def read_tasks_json(value: str) -> list[dict]:
    text = read_payload(value, "--tasks-json")
    try:
        tasks = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TaskflowError(f"--tasks-json is not valid JSON: {exc}") from exc
    if not isinstance(tasks, list) or not all(isinstance(t, dict) for t in tasks):
        raise TaskflowError("--tasks-json must be a JSON array of task objects")
    return tasks


def meta_json(meta) -> str:
    from dataclasses import asdict

    return json.dumps(asdict(meta), ensure_ascii=False)


def print_plan(plan: str) -> None:
    print("--- plan.md ---")
    print(plan.rstrip())
    print("--- end of plan ---")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_create_project(store, args, sync) -> int:
    snap = _snapshot(_project_paths(store, args.project_id))
    meta = create_project(
        store,
        project_id=args.project_id,
        title=args.title,
        source=args.source,
        requester=args.requester,
        parent_task_id=args.parent_task_id,
    )
    _push_with_rollback(store, sync, snap,
                        lambda: sync.push_project(meta.project_id))
    _log("project_created", project=meta.project_id, title=meta.title)
    print(f"[create] project {meta.project_id}: active")
    print(meta_json(meta))
    return 0


def cmd_show_project(store, args, sync) -> int:
    sync.pull_project(args.project_id)
    meta = store.read_project_meta(args.project_id)
    print(meta_json(meta))
    print_plan(store.read_project_plan(args.project_id))
    return 0


def _cmd_lifecycle(store, args, sync, fn, verb) -> int:
    _log("pull", project=args.project_id, backend=sync.describe())
    sync.pull_project(args.project_id)
    snap = _snapshot(_project_paths(store, args.project_id))
    meta = fn(store, project_id=args.project_id)
    _log("project_status", project=meta.project_id, status=meta.status)
    _push_with_rollback(store, sync, snap,
                        lambda: sync.push_project(args.project_id))
    print(f"[{verb}] project {meta.project_id}: {meta.status}")
    print(meta_json(meta))
    return 0


def _cmd_plan_mutation(store, args, sync, fn, verb) -> int:
    _log("pull", project=args.project_id, backend=sync.describe())
    sync.pull_project(args.project_id)
    snap = _snapshot(_project_paths(store, args.project_id))
    fn()
    _log("plan_mutated", project=args.project_id, verb=verb)
    _push_with_rollback(store, sync, snap,
                        lambda: sync.push_project(args.project_id))
    print(f"[{verb}] project {args.project_id}")
    print_plan(store.read_project_plan(args.project_id))
    return 0


def cmd_add_tasks(store, args, sync) -> int:
    tasks = read_tasks_json(args.tasks_json)

    def run():
        add_tasks(store, project_id=args.project_id, tasks=tasks)

    return _cmd_plan_mutation(store, args, sync, run, "add-tasks")


def cmd_plan_dag(store, args, sync) -> int:
    tasks = read_tasks_json(args.tasks_json)

    def run():
        plan_dag(store, project_id=args.project_id, tasks=tasks)

    return _cmd_plan_mutation(store, args, sync, run, "plan-dag")


def cmd_plan_loop(store, args, sync) -> int:
    tasks = read_tasks_json(args.tasks_json) if args.tasks_json else None

    def run():
        plan_loop(
            store,
            project_id=args.project_id,
            goal=args.goal,
            stop_condition=args.stop_condition,
            iteration_template=read_payload(args.iteration_template,
                                            "--iteration-template"),
            max_iterations=args.max_iterations,
            current_iteration=args.current_iteration,
            status=args.status,
            tasks=tasks,
        )

    return _cmd_plan_mutation(store, args, sync, run, "plan-loop")


def cmd_record_iteration(store, args, sync) -> int:
    def run():
        record_loop_iteration(
            store,
            project_id=args.project_id,
            iteration=args.iteration,
            decision=args.decision,
            summary=args.summary,
            next_action=args.next_action,
        )

    return _cmd_plan_mutation(store, args, sync, run, "record-iteration")


def cmd_show_plan(store, args, sync) -> int:
    sync.pull_project(args.project_id)
    print_plan(store.read_project_plan(args.project_id))
    return 0


def cmd_ready(store, args, sync) -> int:
    sync.pull_project(args.project_id)
    plan = store.read_project_plan(args.project_id)
    if parse_plan_type(plan) == "loop":
        nodes = ready_loop_nodes(store, project_id=args.project_id)
    else:
        nodes = ready_nodes(store, project_id=args.project_id)
    if not nodes:
        print("(no ready nodes)")
        return 0
    for task in nodes:
        print(render_dag_task(task))
    return 0


def cmd_delegate(store, args, sync) -> int:
    _log("pull", project=args.project_id, task=args.task_id,
         backend=sync.describe())
    sync.pull_project(args.project_id)
    sync.pull_task(args.task_id, must_exist=False)
    snap = _snapshot(_project_paths(store, args.project_id)
                     + _task_paths(store, args.task_id))
    spec = read_payload(args.spec, "--spec")
    meta = delegate_task(
        store,
        project_id=args.project_id,
        task_id=args.task_id,
        spec=spec,
        room_id=args.room_id,
    )
    _log("status_change", task=args.task_id, from_="pending",
         to=meta.status, assigned_to=meta.assigned_to,
         spec_chars=len(spec))
    _push_with_rollback(
        store, sync, snap,
        lambda: (sync.push_project(args.project_id),
                 sync.push_task(meta.task_id)))
    print(f"[delegate] {meta.task_id}: pending -> {meta.status} "
          f"(spec at shared/tasks/{meta.task_id}/spec.md)")
    print(meta_json(meta))
    print("[next] notify the worker, then run "
          f"'projectflow delegate-commit {args.project_id} {meta.task_id}'")
    return 0


def cmd_delegate_commit(store, args, sync) -> int:
    _log("pull", project=args.project_id, task=args.task_id,
         backend=sync.describe())
    sync.pull_project(args.project_id)
    sync.pull_task(args.task_id)
    snap = _snapshot(_task_paths(store, args.task_id))
    meta = commit_task_assignment(
        store,
        project_id=args.project_id,
        task_id=args.task_id,
        event_id=args.event_id,
    )
    _log("status_change", task=args.task_id, to=meta.status,
         event_id=meta.event_id or None)
    _push_with_rollback(store, sync, snap, lambda: sync.push_task(meta.task_id))
    print(f"[commit] {meta.task_id}: {meta.status} "
          f"(event_id={meta.event_id or '-'})")
    print(meta_json(meta))
    return 0


def cmd_check(store, args, sync) -> int:
    _log("pull", task=args.task_id, backend=sync.describe())
    sync.pull_task(args.task_id)
    meta = store.read_task_meta(args.task_id)
    _log("check_read", task=args.task_id, status=meta.status,
         submitted_at=meta.submitted_at or None)
    print(f"task: {meta.task_id}")
    print(f"project: {meta.project_id}")
    print(f"title: {meta.task_title}")
    print(f"status: {meta.status}")
    print(f"assigned_to: {meta.assigned_to}")
    print(f"room_id: {meta.room_id or '-'}")
    print(f"submitted_at: {meta.submitted_at or '-'}")
    try:
        result = check_task(store, task_id=args.task_id)
        effective = "yes" if is_effective_result(result) else "no"
        print(f"effective_for_acceptance: {effective}")
        print("--- result.md ---")
        print(render_task_result(result).rstrip())
        print("--- end of result ---")
    except TaskflowError:
        print("result: <none>")
    return 0


# ---------------------------------------------------------------------------
# CLI plumbing
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="projectflow",
        description="AgentTeams leader project/task protocol (copaw-compatible)",
    )
    parser.add_argument(
        "--root",
        default=os.environ.get("AGENTTEAMS_FS_ROOT", DEFAULT_FS_ROOT),
        help=f"workspace root containing shared/ (default: {DEFAULT_FS_ROOT} or $AGENTTEAMS_FS_ROOT)",
    )
    parser.add_argument(
        "--sync",
        choices=["none", "mc"],
        default="mc",
        help="sync backend: mc = MinIO via the copaw filesync env contract "
             "(default, the deployed form); none = local shared/ only (tests)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-project", help="create project meta + empty DAG plan")
    p.add_argument("project_id")
    p.add_argument("--title", required=True)
    p.add_argument("--source")
    p.add_argument("--requester")
    p.add_argument("--parent-task-id")

    p = sub.add_parser("show-project", help="print project meta + plan.md")
    p.add_argument("project_id")

    for verb, fn, help_ in (
        ("pause-project", pause_project, "pause DAG scheduling"),
        ("resume-project", resume_project, "resume DAG scheduling"),
        ("complete-project", complete_project, "mark project completed"),
    ):
        p = sub.add_parser(verb, help=help_)
        p.add_argument("project_id")

    p = sub.add_parser("add-tasks", help="add/update pending DAG tasks")
    p.add_argument("project_id")
    p.add_argument("--tasks-json", required=True,
                   help="JSON array [{taskId,title,assignedTo,dependsOn}]; literal, @file, or -")

    p = sub.add_parser("plan-dag", help="replace the DAG with the latest graph shape")
    p.add_argument("project_id")
    p.add_argument("--tasks-json", required=True)

    p = sub.add_parser("plan-loop", help="replace the plan with a Loop plan")
    p.add_argument("project_id")
    p.add_argument("--goal", required=True)
    p.add_argument("--stop-condition", required=True)
    p.add_argument("--iteration-template", required=True,
                   help="template text; literal, @file, or -")
    p.add_argument("--max-iterations", type=int, required=True)
    p.add_argument("--current-iteration", type=int, default=0)
    p.add_argument("--status", default="running")
    p.add_argument("--tasks-json", default=None)

    p = sub.add_parser("record-iteration", help="record a Leader loop decision")
    p.add_argument("project_id")
    p.add_argument("--iteration", type=int, required=True)
    p.add_argument("--decision", required=True,
                   help="continue | replan | ask_user | stop_success | stop_blocked")
    p.add_argument("--summary", required=True)
    p.add_argument("--next-action")

    p = sub.add_parser("show-plan", help="print plan.md")
    p.add_argument("project_id")

    p = sub.add_parser("ready", help="list ready nodes (dag or loop by plan type)")
    p.add_argument("project_id")

    p = sub.add_parser("delegate", help="prepare a task: write meta+spec, mark node delegated")
    p.add_argument("project_id")
    p.add_argument("task_id")
    p.add_argument("--spec", required=True, help="spec text; literal, @file, or -")
    p.add_argument("--room-id", default=None,
                   help="task room; workers' ack requires it — pass it when delegating")

    p = sub.add_parser("delegate-commit", help="mark the prepared task assigned (after notifying)")
    p.add_argument("project_id")
    p.add_argument("task_id")
    p.add_argument("--event-id", default=None)

    p = sub.add_parser("check", help="leader read: task meta + result.md")
    p.add_argument("task_id")

    return parser


def main(argv: list[str] | None = None) -> int:
    global LOG
    # Output contract is UTF-8/LF; don't let the host locale re-encode.
    for stream in (sys.stdin, sys.stdout):
        stream.reconfigure(encoding="utf-8", newline="\n")
    started = time.monotonic()
    args = build_parser().parse_args(argv)
    LOG = agentteams_log.setup("projectflow",
                               argv if argv is not None else sys.argv[1:])
    _log("context", root=args.root, sync=args.sync)
    store = FileSystemTaskStore(args.root)
    code = 1
    try:
        sync = make_sync_backend(args.sync, Path(args.root))
        if args.command == "create-project":
            code = cmd_create_project(store, args, sync)
        elif args.command == "show-project":
            code = cmd_show_project(store, args, sync)
        elif args.command == "pause-project":
            code = _cmd_lifecycle(store, args, sync, pause_project, "pause")
        elif args.command == "resume-project":
            code = _cmd_lifecycle(store, args, sync, resume_project, "resume")
        elif args.command == "complete-project":
            code = _cmd_lifecycle(store, args, sync, complete_project, "complete")
        elif args.command == "add-tasks":
            code = cmd_add_tasks(store, args, sync)
        elif args.command == "plan-dag":
            code = cmd_plan_dag(store, args, sync)
        elif args.command == "plan-loop":
            code = cmd_plan_loop(store, args, sync)
        elif args.command == "record-iteration":
            code = cmd_record_iteration(store, args, sync)
        elif args.command == "show-plan":
            code = cmd_show_plan(store, args, sync)
        elif args.command == "ready":
            code = cmd_ready(store, args, sync)
        elif args.command == "delegate":
            code = cmd_delegate(store, args, sync)
        elif args.command == "delegate-commit":
            code = cmd_delegate_commit(store, args, sync)
        elif args.command == "check":
            code = cmd_check(store, args, sync)
        else:
            raise TaskflowError(f"unknown command: {args.command}")
    except TaskflowError as exc:
        _log("error", level=logging.ERROR, error=str(exc))
        print(f"[projectflow] error: {exc}", file=sys.stderr)
        return 1
    except McSyncError as exc:
        _log("error", level=logging.ERROR, error=str(exc))
        print(f"[projectflow] error: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr or exc.stdout or ""
        _log("error", level=logging.ERROR, error="mc command failed",
             mc_returncode=exc.returncode)
        print(f"[projectflow] mc command failed ({exc.returncode}): "
              f"{detail.strip()[:500]}", file=sys.stderr)
        return 1
    except (OSError, RuntimeError) as exc:
        _log("error", level=logging.ERROR, error=str(exc))
        print(f"[projectflow] sync error: {exc}", file=sys.stderr)
        return 1
    finally:
        agentteams_log.log_cmd_end(LOG, code if code == 0 else 1, started,
                                   command=args.command,
                                   project=getattr(args, "project_id", None),
                                   task=getattr(args, "task_id", None))
    return code


if __name__ == "__main__":
    sys.exit(main())
