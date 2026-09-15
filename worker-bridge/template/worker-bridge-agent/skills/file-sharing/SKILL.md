---
name: file-sharing
description: Use before agentteams-sync calls, reading non-task shared files, pushing mid-task progress, or troubleshooting missing shared files. Do not use for normal task acceptance or submission; taskflow ack and taskflow submit handle lifecycle sync internally.
---

# File Sharing

Use local shared paths only. Do not expose storage internals.

## The Command

`agentteams-sync` is a bash command backed by `~/skills/file-sharing/scripts/agentteams_sync.py`. Credentials and workspace root come from the environment — never pass them yourself. It prints one JSON line (`{"ok": true, ...}` / `{"ok": false, "error": ...}`) and exits non-zero on failure.

```bash
agentteams-sync pull  <path>                     # shared/... -> local workspace
agentteams-sync push  <path> [--exclude <glob>]  # local -> shared/...
agentteams-sync stat  <path>                     # verify a path exists on storage
agentteams-sync list  <path>                     # list a shared directory
```

If `agentteams-sync` is not on PATH: `python3 ~/skills/file-sharing/scripts/agentteams_sync.py <args>`.

## Local Paths

Use:

- `shared/tasks/{task-id}/`
- `shared/projects/{project-id}/` only for read-only project context

Directory sync paths must end with `/`. (`shared/tasks/{task-id}` without the slash is still treated as a directory automatically, but write it with the slash.) If a directory pull fails, report the `agentteams-sync` result instead of creating local placeholder directories or switching to absolute paths.

Do not use in chat, task outputs, or normal reasoning:

- `agentteams/agentteams-storage/...`
- `teams/{team}/shared/...`
- `/root/agentteams-fs/...`
- `/root/agentteams-fs/agents/...`

## Task Lifecycle Sync

`taskflow ack {task-id}` and `taskflow submit {task-id}` handle all task lifecycle file sync internally (pull, push, stat). Do not call `agentteams-sync` separately for these operations.

## Non-Task Shared Files

For shared files outside the task lifecycle (project context, reference materials), pull before reading:

```bash
agentteams-sync pull shared/projects/{project-id}/
```

To push mid-task progress or non-result files that the coordinator needs before submission:

```bash
agentteams-sync push shared/tasks/{task-id}/progress/ --exclude spec.md --exclude base/
```

## If You Cannot Find Files

1. Pull the task directory:

   ```bash
   agentteams-sync pull shared/tasks/{task-id}/
   ```

2. Check `pwd`, then check the local relative path from the task message:

   ```bash
   pwd
   ls -la
   ls -la shared/tasks/{task-id}/
   ```

3. If still missing, @mention your coordinator with the `agentteams-sync` JSON output and the exact local path you checked:

```text
@coordinator:domain BLOCKED: I pulled shared/tasks/{task-id}/ but cannot find shared/tasks/{task-id}/spec.md.
```

Do not search random container absolute paths or create the missing task directory yourself.
