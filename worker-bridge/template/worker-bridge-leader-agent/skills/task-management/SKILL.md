---
name: task-management
description: Use for project creation, DAG/loop planning, readiness checks, task delegation, acceptance of worker results, and project lifecycle control (pause/resume/complete). Leader role only.
---

# Task Management (Leader)

Project and task protocol commands. Everything is a bash command; the state
lives in `shared/projects/<id>/` (meta.json + plan.md) and
`shared/tasks/<id>/` (meta.json + spec.md + result.md) — never hand-edit
those files.

## Project lifecycle

```
projectflow create-project <project-id> --title "<title>" [--source <s>] [--requester <r>]
projectflow show-project  <project-id>
projectflow pause-project  <project-id>
projectflow resume-project <project-id>
projectflow complete-project <project-id>
```

## Planning

```
projectflow add-tasks <project-id> --tasks-json '<JSON array>'
projectflow plan-dag  <project-id> --tasks-json '<JSON array>'
projectflow plan-loop <project-id> --goal "<g>" --stop-condition "<c>" \
    --iteration-template "<text|@file>" --max-iterations <n> [--tasks-json '<array>']
projectflow record-iteration <project-id> --iteration <n> \
    --decision continue|replan|ask_user|stop_success|stop_blocked \
    --summary "<one line>" [--next-action "<next>"]
projectflow show-plan <project-id>
```

Task object shape (both `add-tasks` and `plan-*`):

```json
[{"taskId": "t-1", "title": "Design API", "assignedTo": "@bob:example.org",
  "dependsOn": []}]
```

- `add-tasks` = additive: adds new nodes, refuses to modify non-pending ones.
- `plan-dag` = replace: reshapes the graph, preserving existing node status.
- Long payloads: pass `@path` to read from a file, or `-` for stdin.
- Cycles, unknown dependencies and duplicate ids are rejected.

## Scheduling and delegation

```
projectflow ready <project-id>              # ready nodes (dag or loop, auto)
projectflow delegate <project-id> <task-id> --spec "<spec text|@file>" \
    [--room-id "!room:example.org"]
projectflow delegate-commit <project-id> <task-id> [--event-id "<matrix event>"]
```

Delegation sequence (respect the order):

1. `ready` → pick a pending node whose dependencies are completed.
2. `delegate` → writes task meta + spec and marks the node delegated.
3. Notify the worker in the task room (this is YOUR message, not a command).
4. `delegate-commit` → marks the task assigned (records the event id so a
   retry never double-notifies).

Always pass `--room-id` when delegating: workers cannot acknowledge a task
without it.

## Reading worker results

```
taskflow check <task-id>          # worker-facing summary
projectflow check <task-id>       # leader read: meta + full result.md
```

`projectflow check` prints `effective_for_acceptance: yes|no`
(yes = `SUCCESS` / `SUCCESS_WITH_NOTES`). Accept a result by marking the
node completed in the plan (`plan-dag` / `plan-loop` with the node's status
preserved through re-planning happens automatically for kept ids); reject
by re-planning or re-delegating per your judgment.

## Rules

- Run `ready` before each delegation round; never delegate from memory.
- One delegation per node at a time; a delegated node disappears from `ready`.
- Paused projects return no ready nodes and reject delegation.
- Do not hand-edit `plan.md`, `meta.json`, `spec.md`, or `result.md`.
