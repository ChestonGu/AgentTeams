---
name: task-management
description: Use before any Worker taskflow command or assigned-task workflow, including reading task state, acknowledging a task, executing a task, tracking progress, handling blockers/questions, submitting structured results, or reporting completion. Always use this skill when the message mentions assigned task, task ID, shared/tasks, spec.md, meta.json, result.md, deliverables, BLOCKED, REVISION_NEEDED, SUCCESS, taskflow, ack, or submit.
---

# Task Management

You are a Worker. Execute only your assigned task.

## The Command

`taskflow` is a bash command backed by `~/skills/task-management/scripts/taskflow.py`. Your Matrix identity and workspace root come from the environment (`AGENTTEAMS_MATRIX_USER_ID`, `AGENTTEAMS_FS_ROOT`) — never pass them yourself.

```bash
taskflow check  {task-id}                                     # read-only status summary
taskflow ack    {task-id}                                     # accept; output contains the spec
taskflow submit {task-id} --status <STATUS> --summary "<one paragraph>" \
                [--deliverables workspace/<file> ...] [--notes "<note>" ...]
```

If `taskflow` is not on PATH: `python3 ~/skills/task-management/scripts/taskflow.py <args>`.

## Task Directory

All work for a task stays under:

```text
shared/tasks/{task-id}/
```

Your coordinator creates:

```text
shared/tasks/{task-id}/spec.md
shared/tasks/{task-id}/meta.json
shared/tasks/{task-id}/base/
```

You own:

```text
shared/tasks/{task-id}/workspace/
shared/tasks/{task-id}/progress/
shared/tasks/{task-id}/<deliverables>
```

`taskflow` owns `shared/tasks/{task-id}/result.md` and `meta.json`. Do not hand-edit either file. You submit task results through the `taskflow submit` command; it writes the standard `result.md` protocol for you.

`taskflow ack` and `taskflow submit` only succeed when your Matrix identity matches `meta.json.assigned_to`. If either command reports that the task is assigned to someone else, stop and report the assignment mismatch to your coordinator.

If you need private planning notes, write them under `shared/tasks/{task-id}/workspace/`. Do not create shared task-level `plan.md`.

Do not edit project-level `shared/projects/{project-id}/plan.md` or `meta.json` unless the task spec explicitly tells you to.

## Execution Flow

1. In the current session, directly say that you received the message before task acceptance work starts.
2. Accept the task:

   ```bash
   taskflow ack {task-id}
   ```

   This single command pulls the task directory from storage, reads `spec.md` and `meta.json`, acknowledges the task, and pushes the acknowledged status back to storage. The output contains the full task spec — read it from the output instead of opening `spec.md` separately.

   `meta.json.room_id` is the task's assignment and delivery room. Use it only when cross-room delivery is truly needed. If it is missing, stop and report a blocker in the current session instead of guessing another room.

3. Execute the task. Keep deliverables inside `shared/tasks/{task-id}/`.
4. Submit the task result:

   ```bash
   taskflow submit {task-id} \
     --status SUCCESS \
     --summary "<one paragraph summary>" \
     --deliverables workspace/<file>
   ```

   This writes `shared/tasks/{task-id}/result.md`, marks local task state submitted, pushes the task directory to storage, and verifies `result.md` exists on storage. Bare deliverable paths (like `workspace/<file>`) are prefixed with `shared/tasks/{task-id}/` automatically; full `shared/...` paths are kept as-is.

   Use `SUCCESS`, `SUCCESS_WITH_NOTES`, `REVISION_NEEDED`, or `BLOCKED` for normal task execution status.

   Submitting a result ends this Worker task. If more work is needed after `REVISION_NEEDED` or `BLOCKED`, wait for your coordinator to assign a new task; do not resume or rewrite the submitted task on your own.

5. In the current session, directly @mention your coordinator with completion:

   ```text
   @coordinator:domain TASK_COMPLETED: {task-id} - <short outcome>. Result: shared/tasks/{task-id}/result.md
   ```

   Do not look up your Worker profile room or private room as a fallback. The task directory is the source of truth if you ever need to verify the assignment room.

## Blocked

If blocked, submit a `BLOCKED` result. `taskflow submit` automatically pushes and verifies:

```bash
taskflow submit {task-id} --status BLOCKED --summary "<what is blocking you>"
```

Then @mention your coordinator:

```text
@coordinator:domain BLOCKED: {task-id} - <what is blocking you>
```

Do not invent missing task files, project plans, or shared directories.

## Progress

Progress notes are optional unless the task spec asks for them. If you write progress, put it under:

```text
shared/tasks/{task-id}/progress/YYYY-MM-DD.md
```

Progress updates that require no decision should not @mention anyone.
