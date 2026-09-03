"""Local taskflow state machine for the opencode AgentTeams worker.

Vendored from ``AgentTeams/copaw/src/copaw_worker/task.py`` (dev-v1.2.2)
with the leader/manager-only surface removed (project meta, DAG/loop plan,
delegate/prepare/commit). The worker-side functions below are kept
line-equivalent to the original so protocol output (meta.json, result.md)
stays byte-compatible with copaw workers.

Removed from the original: ProjectMeta, DagTask, LoopPlan and every
create_project / add_tasks / plan_dag / plan_loop / ready_* / delegate_*
/ pause / resume / complete function. Kept: TaskMeta, TaskResult, the
task-file portion of FileSystemTaskStore, ack/submit/check, result.md
parse/render/validate, and identity normalization.

Deliberate deviation (the only one): every file read/write passes
``encoding="utf-8"`` explicitly (the original relies on the platform
default, which is UTF-8 in the Linux sandbox but GBK on the Windows dev
machines), and every write passes ``newline="\n"`` so protocol files carry
LF line endings on every platform — identical to what the Linux copaw
original produces. Without it, a Windows run would upload CRLF files to
MinIO for the Leader to read.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any


class TaskflowError(ValueError):
    """Expected user-facing taskflow error."""


RESULT_STATUSES = {
    "SUCCESS",
    "SUCCESS_WITH_NOTES",
    "REVISION_NEEDED",
    "BLOCKED",
    "INTERRUPTED",
}
EFFECTIVE_RESULT_STATUSES = {"SUCCESS", "SUCCESS_WITH_NOTES"}


@dataclass
class TaskMeta:
    task_id: str
    project_id: str
    task_title: str
    assigned_to: str
    room_id: str | None = None
    status: str = "assigned"
    depends_on: list[str] = field(default_factory=list)
    assigned_at: str | None = None
    acknowledged_at: str | None = None
    submitted_at: str | None = None
    event_id: str | None = None


@dataclass(frozen=True)
class TaskResult:
    status: str
    summary: str
    deliverables: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class FileSystemTaskStore:
    """Task-file storage backed by the local ``shared/`` directory."""

    def __init__(self, workspace_dir: Path | str | None = None) -> None:
        self.workspace_dir = Path(workspace_dir) if workspace_dir else Path.cwd()
        self.shared_dir = self.workspace_dir / "shared"

    def _task_dir(self, task_id: str) -> Path:
        return self.shared_dir / "tasks" / _safe_id(task_id)

    def read_task_meta(self, task_id: str) -> TaskMeta:
        path = self._task_dir(task_id) / "meta.json"
        data = _read_json(path)
        return TaskMeta(
            task_id=str(data["task_id"]),
            project_id=str(data["project_id"]),
            task_title=str(data["task_title"]),
            assigned_to=str(data["assigned_to"]),
            room_id=data.get("room_id"),
            status=str(data.get("status") or "assigned"),
            depends_on=list(data.get("depends_on") or []),
            assigned_at=data.get("assigned_at"),
            acknowledged_at=data.get("acknowledged_at"),
            submitted_at=data.get("submitted_at"),
            event_id=data.get("event_id"),
        )

    def write_task_meta(self, meta: TaskMeta) -> None:
        path = self._task_dir(meta.task_id) / "meta.json"
        _write_json(path, _drop_none(asdict(meta)))

    def read_task_spec(self, task_id: str) -> str:
        path = self._task_dir(task_id) / "spec.md"
        if not path.exists():
            raise TaskflowError(f"task spec not found: {path}")
        return path.read_text(encoding="utf-8")

    def write_task_spec(self, task_id: str, spec: str) -> None:
        path = self._task_dir(task_id) / "spec.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(spec, encoding="utf-8", newline="\n")

    def read_task_result(self, task_id: str) -> TaskResult:
        path = self._task_dir(task_id) / "result.md"
        if not path.exists():
            raise TaskflowError(f"task result not found: {path}")
        result = parse_task_result(path.read_text(encoding="utf-8"))
        validate_task_result(task_id, result)
        return result

    def write_task_result(self, task_id: str, result: TaskResult) -> None:
        validate_task_result(task_id, result)
        path = self._task_dir(task_id) / "result.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_task_result(result), encoding="utf-8", newline="\n")


def check_task(store: FileSystemTaskStore, *, task_id: str) -> TaskResult:
    """Read and validate a submitted task result without changing state."""
    return store.read_task_result(task_id)


def is_effective_result(result: TaskResult) -> bool:
    """Return whether a task result is a candidate for Leader acceptance."""
    return result.status in EFFECTIVE_RESULT_STATUSES


def ack_task(
    store: FileSystemTaskStore, *, task_id: str, actor: str | None = None
) -> TaskMeta:
    """Mark a local task as acknowledged/in progress without touching graph."""
    meta = store.read_task_meta(task_id)
    _require_assigned_worker(meta, actor)
    _require_task_room(meta)
    meta.status = "in_progress"
    meta.acknowledged_at = meta.acknowledged_at or _now()
    store.write_task_meta(meta)
    return meta


def submit_task(
    store: FileSystemTaskStore,
    *,
    task_id: str,
    result: TaskResult | None = None,
    actor: str | None = None,
) -> TaskMeta:
    """Mark a local task submitted after result.md exists and is valid."""
    meta = store.read_task_meta(task_id)
    _require_assigned_worker(meta, actor)
    _require_task_room(meta)
    if result is not None:
        store.write_task_result(task_id, result)
    else:
        store.read_task_result(task_id)
    meta.status = "submitted"
    meta.submitted_at = _now()
    store.write_task_meta(meta)
    return meta


def _require_assigned_worker(meta: TaskMeta, actor: str | None) -> None:
    current = canonical_worker_id(actor)
    if not current:
        raise TaskflowError("current worker identity is required")
    assigned = canonical_worker_id(meta.assigned_to)
    if current != assigned:
        raise TaskflowError(
            f"task {meta.task_id} is assigned to {meta.assigned_to}, not {current}",
        )


def _require_task_room(meta: TaskMeta) -> None:
    if not (meta.room_id or "").strip():
        raise TaskflowError(f"task {meta.task_id} is missing room_id")


def canonical_worker_id(value: str | None) -> str:
    """Normalize Matrix/display worker identities to the logical worker name."""
    text = (value or "").strip()
    if not text:
        return ""

    token = text.split()[0].strip()
    token = token.strip("`'\"")
    token = token.removeprefix("@")
    if ":" in token:
        token = token.split(":", 1)[0]
    return token.strip(":,;")


def parse_task_result(text: str) -> TaskResult:
    status = ""
    summary = ""
    deliverables: list[str] = []
    notes: list[str] = []
    section = ""

    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("STATUS:"):
            status = line[len("STATUS:") :].strip()
            section = ""
            continue
        if line.startswith("SUMMARY:"):
            summary = line[len("SUMMARY:") :].strip()
            section = ""
            continue
        if line == "DELIVERABLES:":
            section = "deliverables"
            continue
        if line == "NOTES:":
            section = "notes"
            continue
        if line.startswith("- "):
            item = line[2:].strip()
            if section == "deliverables":
                deliverables.append(item)
            elif section == "notes":
                notes.append(item)

    if status not in RESULT_STATUSES:
        raise TaskflowError(f"invalid result status: {status or '<missing>'}")
    if not summary:
        raise TaskflowError("result summary is required")
    return TaskResult(status=status, summary=summary, deliverables=deliverables, notes=notes)


def render_task_result(result: TaskResult) -> str:
    lines = [
        f"STATUS: {result.status}",
        f"SUMMARY: {_single_line(result.summary)}",
        "",
        "DELIVERABLES:",
    ]
    lines.extend(f"- {item}" for item in result.deliverables)
    if result.notes:
        lines.extend(["", "NOTES:"])
        lines.extend(f"- {item}" for item in result.notes)
    return "\n".join(lines).rstrip() + "\n"


def validate_task_result(task_id: str, result: TaskResult) -> None:
    if result.status not in RESULT_STATUSES:
        raise TaskflowError(f"invalid result status: {result.status or '<missing>'}")
    if not result.summary.strip():
        raise TaskflowError("result summary is required")
    prefix = f"shared/tasks/{_safe_id(task_id)}/"
    for path in result.deliverables:
        if not isinstance(path, str) or not path.strip():
            raise TaskflowError("deliverable path must be a non-empty string")
        if not path.startswith(prefix):
            raise TaskflowError(
                f"deliverable must be under {prefix}: {path}",
            )
        parts = Path(path).parts
        if any(part in ("", ".", "..") for part in parts):
            raise TaskflowError(f"invalid deliverable path: {path}")


def _safe_id(value: str) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", text):
        raise TaskflowError(f"invalid id: {value}")
    return text


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _single_line(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise TaskflowError(f"file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _drop_none(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if value is not None}
