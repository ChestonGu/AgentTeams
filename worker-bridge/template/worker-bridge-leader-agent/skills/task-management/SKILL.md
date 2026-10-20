---
name: task-management
description: Use for project creation, DAG/loop planning, ready-node checks, task delegation and assignment notifications, worker result checks and acceptance, loop decisions, and project lifecycle (pause/resume/complete). Leader role only.
---

# Task Management (Leader)

Project and task protocol state lives in `shared/projects/<id>/` (meta.json + plan.md) and `shared/tasks/<id>/` (meta.json + spec.md + result.md). Change it only through `projectflow`, plus the single sanctioned acceptance-marker edit described below.

## The Command

`projectflow` is a bash command backed by `/opt/agentteams/skills/task-management/scripts/projectflow.py`. Every command pulls what it needs from storage, mutates, and pushes back; on push failure local files roll back to their pre-command state. Your workspace root comes from the environment (`AGENTTEAMS_FS_ROOT`) — never pass it yourself.

If `projectflow` is not on PATH: `python3 /opt/agentteams/skills/task-management/scripts/projectflow.py <args>`.

## Verbs

```bash
projectflow create-project <pid> --title "<title>" [--source <s>] [--requester <mxid>] [--parent-task-id <tid>]
projectflow show-project  <pid>
projectflow pause-project  <pid>        # paused projects return no ready nodes
projectflow resume-project <pid>
projectflow complete-project <pid>      # after final project result files are written

projectflow add-tasks  <pid> --tasks-json '<JSON array | @file | ->'
projectflow plan-dag   <pid> --tasks-json '<JSON array | @file | ->'
projectflow plan-loop  <pid> --goal "<goal>" --stop-condition "<condition>" \
    --iteration-template "<text | @file>" --max-iterations <n> [--tasks-json '<array>']
projectflow record-iteration <pid> --iteration <n> \
    --decision continue|replan|ask_user|stop_success|stop_blocked \
    --summary "<one line>" [--next-action "<next step>"]
projectflow show-plan  <pid>

projectflow ready           <pid>                          # ready nodes (dag or loop, auto)
projectflow delegate        <pid> <tid> --spec "<spec | @file>" --room-id "<Team Room>"
projectflow delegate-commit <pid> <tid> --assignee-mxid <@worker:domain> [--no-notify]
projectflow check           <tid>                          # leader read: meta + full result.md
```

Task object shape (`add-tasks` / `plan-dag` / `plan-loop`):

```json
[{"taskId": "collect-1", "title": "Collect source data", "assignedTo": "<assignee>", "dependsOn": []}]
```

- `assignedTo` records who executes the node; make it unambiguous — the canonical assignment identity is the full Matrix ID you pass to `delegate-commit --assignee-mxid`.
- `add-tasks` is additive: it adds nodes and updates pending ones only. `plan-dag` / `plan-loop` replace the graph: kept ids keep their status, obsolete ids are dropped by omission, new ids start pending.
- Long payloads: `@path` reads a file, `-` reads stdin. Cycles, unknown dependencies, and duplicate ids are rejected.
- Project ids follow `{description}-YYYYMMDD-HHMMSS`; task ids follow `{projectId}-{seq}`.

## Delegation (three steps, in order)

1. `projectflow ready <pid>` — returns pending nodes whose dependencies are completed. Delegate only ids `ready` returned; never delegate from memory.
2. `projectflow delegate <pid> <tid> --spec "<spec>" --room-id "<Team Room ID>"` — writes meta.json + spec.md and marks the node delegated. `--room-id` is required for team work: the Team Room ID from the Coordination block of your agent.md — the room where the Worker acknowledges and reports completion.
3. `projectflow delegate-commit <pid> <tid> --assignee-mxid <@worker:domain>` — marks the task assigned and prints the complete assignment text between `---- assignment text begin ----` / `---- assignment text end ----` markers: the Worker's full Matrix ID as a leading @mention, the task title, and the spec preview. Paste that text verbatim into your session reply — the bridge converts the @ token to the structured mention that wakes the Worker (see AGENTS.md Delegation delivery). `--assignee-mxid` is the full Matrix ID from the Coordination block's Team Workers list.

After a successful `delegate-commit`, the assignment is delivered only when the printed text is pasted verbatim into your session reply — do not edit, shorten, or reword it, and never send a second assignment message afterwards. `--no-notify` skips the assignment print (manual bookkeeping path paired with `--event-id`); use it only for recovery when told to.

Write the spec in the language selected by your AGENTS.md Response Language. The spec is Worker-facing: goal, context, constraints, and expected result. Keep deliverables under `shared/tasks/<tid>/`; require a `result.md` with STATUS, SUMMARY, DELIVERABLES; when you need the Worker to report back, instruct them to @mention your full Matrix ID on completion. Do not prescribe Worker-internal steps — their acknowledgement, sync, submit, or planning belongs to the Worker's own runtime, not the spec.

## Checking And Accepting Results (TASK_COMPLETED)

1. On a Worker TASK_COMPLETED notice, run `projectflow check <tid>`. It pulls the task directory and prints the task meta, `effective_for_acceptance: yes|no`, and the full result.md.
2. `effective_for_acceptance` is `yes` for `SUCCESS` / `SUCCESS_WITH_NOTES` — those are candidate results, not acceptance. Only your acceptance completes a node; a Worker `SUCCESS` alone unblocks nothing. `SUCCESS_WITH_NOTES` carries Worker notes: read them and reflect them in your acceptance report. The other result statuses are never candidates: `REVISION_NEEDED` → step 4 (revision work gets a new task id); `BLOCKED` → step 4 plus report the blocker to the requester (a blocked node may call for a replacement assignment); `INTERRUPTED` → the task never finished — re-delegate the work as a new node.
3. Accept: pull the project directory (`agentteams-sync pull shared/projects/<pid>/`), set the node's marker to `[x]` in `shared/projects/<pid>/plan.md` — the sanctioned acceptance edit, that one marker and nothing else — push it back (`agentteams-sync push shared/projects/<pid>/`), then run `projectflow ready <pid>` to resolve the next wave.
4. Reject, revise, or extend: do not mark `[x]`. Re-shape the graph with `plan-dag` / `plan-loop` (drop obsolete ids, add new pending ids — a delegated node cannot reset to pending, so revision work gets a NEW task id) and delegate the new nodes normally.
5. Loop plans: accept nodes with the same marker edit; record iteration decisions with `record-iteration` and shape the next iteration's task set with `plan-loop`.

Plan markers: `[ ]` pending, `[~]` delegated, `[x]` completed / accepted by you, `[!]` blocked, `[→]` revision. Only `[x]` satisfies dependencies.

## Rules

- Run `ready` before each delegation round; one delegation per node at a time (a delegated node disappears from `ready`).
- Paused projects return no ready nodes and reject delegation; resume with `resume-project` before scheduling again.
- A result arriving for a paused project waits: confirm with `show-project` first — do not accept or mark `[x]`, tell the requester the project is paused and what resume depends on, and hold until they resume it.
- Do not hand-edit meta.json, spec.md, result.md, or plan.md structure; the only manual write is the acceptance marker — setting a node's marker to `[x]` (step 3 above).
- If a shared directory pull fails, stop and report the sync failure; do not fall back to partial file pulls and then mutate project state.
