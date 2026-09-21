---
name: organization
description: Use only when you need to look up team topology, worker phase, or identity that is NOT available from the current message context or the Coordination block of AGENTS.md. Do not use for standard task flows — the coordinator is the message sender, and the task room is in meta.json.room_id.
---

# Organization

Use this skill for AgentTeams topology and identity questions.

## Source Of Truth

The **Coordination block of AGENTS.md** (between the `agentteams-team-context` markers) is the controller-maintained source of team facts — your coordinator, Team Admin, coordinator members, the Team Roster (every member with their Matrix ID, role, and responsibility description), and reporting rules. The controller re-injects it on team changes, so it stays current. Do not infer current state from memory, old chat history, or old task files.

Check, in order:

1. The current message (the coordinator is its sender).
2. `shared/tasks/{task-id}/meta.json` (assignment and delivery room).
3. The Coordination block of AGENTS.md (coordinator / admin / coordinator members).

## What To Use It For

- Confirm who your coordinator, Team Admin, or coordinator members are (Matrix IDs are in the Coordination block)
- Confirm a coworker's Matrix ID, role, or responsibility (the Team Roster in the Coordination block lists every member with description)
- Confirm your own identity (the Environment section at the end of AGENTS.md)
- Confirm room IDs when asked to reason about routing

A teammate whose entry is missing from the Team Roster (e.g. joined before the roster landed, or their Matrix ID had not registered yet) is resolved by asking your coordinator. Do not guess.

Do not use your Worker profile room or private room as the delivery target for a task result. Task completion routing comes from `shared/tasks/{task-id}/meta.json.room_id`.

If required identity or room metadata is missing from all three sources above, ask your coordinator. Do not guess.
