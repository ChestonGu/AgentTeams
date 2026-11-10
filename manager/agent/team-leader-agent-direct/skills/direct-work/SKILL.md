---
name: direct-work
description: Execute direct @mention work in a direct-collaboration team — deliverables in shared/outputs/<worker>/, work_report status events, Team Room completion reply. Use before starting @requested work, writing deliverables, pushing results, or reporting completion. Replaces project-management/task-management in direct teams.
---

# Direct Work

Your team runs in direct collaboration mode: the requester's @mention is the work specification, and you execute it yourself — never by delegating to workers. There is no task protocol — no task IDs, no ack/submit, no plan DAG. This skill owns three things: where deliverables live, how they reach shared storage, and how you report status.

## Deliverable Layout

Write every deliverable under your own outputs directory:

```
shared/outputs/<your-worker-name>/<file-or-subdir>
```

- Your worker name is your AgentTeams worker identity (the Coordination block and runtime context carry it).
- One request may produce one file or a subdirectory of files — your choice; keep names descriptive.
- Never write into another member's outputs directory. Shared storage overwrites same-named files without merging, so per-worker directories are what keeps parallel work safe.
- Keep scratch work, notes, and intermediate files in your own workspace, outside `shared/outputs/`.

## Push Deliverables

After writing deliverables, push your outputs directory to team shared storage:

```
filesync(action="push", path="shared/outputs/<your-worker-name>/")
```

Read the `file-sharing` skill first if this is your first sync in the session or the push fails. Completion replies reference only shared-relative paths (`shared/outputs/...`), never container-absolute or remote storage paths.

## Status Reporting (work_report)

Report through the `work_report` tool. Events go to the team STATUS_EVENT stream (pod log + optional webhook).

When you start real work (optional for quick requests, recommended for multi-step or long work):

```
work_report(action="started", summary="<one sentence>", requester="<requester-matrix-id>")
```

When the work completes (required whenever you produced a deliverable or a final outcome):

```
work_report(action="completed", summary="<one sentence>",
            deliverables=["shared/outputs/<your-worker-name>/<file>"],
            requester="<requester-matrix-id>", status="SUCCESS")
```

- `status`: `SUCCESS` (default), `SUCCESS_WITH_NOTES`, or `BLOCKED` (only for a real blocker, not a credential wait).
- `requester` is the full Matrix ID of the member who @mentioned you.
- Pure Q&A with no deliverables needs no work_report calls.
- Reporting is best-effort: a failed report never blocks your work or your reply.

## Completion Reply

Finish with ONE Team Room message that @mentions the requester (full Matrix ID) and contains the outcome in one or two sentences plus the shared-relative deliverable paths. Reports go to the Team Room even when the request arrived in your Leader DM.

## Media Archiving (leader-only)

You remain the team's single media archivist: room media lands in `shared/knowledge/matrix/` under your watch. This is independent of direct work — do not skip it, and do not ask other members to archive.

## Credential Waits

Follow AGENTS.md Authorization Failures exactly: check `shared/oauth/` locally, pull once, then report 「{system} 暂无可用授权，请配置后让我重试」 to the **requester** and pause. Do not report BLOCKED to `work_report` for a credential wait; resume the same work when access is ready.
