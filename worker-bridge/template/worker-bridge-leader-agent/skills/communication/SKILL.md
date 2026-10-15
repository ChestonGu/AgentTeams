---
name: communication
description: Use before deciding whether to reply, sending requester updates, making @mention decisions, handling assignment notifications, Leader DM requests, or preventing reply loops.
---

# Communication

## Routing

Your session reply reaches only the room the triggering message came from. There is no send command in your toolchain.

| Message | Target | How |
|---|---|---|
| Direct answer, question reply, clarification | Current room | Reply directly in the current session |
| Worker task assignment | Team Room | `projectflow delegate-commit --assignee-mxid ...` sends it automatically — never hand-write it |
| Requester report (state change, completion, blocker, decision) | Team Room, @requester + @Team Admin | Session reply when the triggering message is in the Team Room (worker completions arrive there) |
| Human conversation from Leader DM | Leader DM | Reply directly in the DM session |

## Assignment Notifications

`delegate-commit` delivers the assignment: a structured @mention of the Worker's full Matrix ID, a per-task transaction id so retries never double-send, and the Matrix event id recorded on the task. After a successful commit, do not send a second assignment message — it duplicates the assignment and can trigger the Worker twice.

An assignment intent sentence is not an assignment. "I will assign this to the dev worker", "now delegate the first ready node", "the dev worker should start" — none of these notify anyone.

## Requester Reports (Dual Anchor)

Requester-facing Team Room reports @mention BOTH the requester and the Team Admin by full Matrix IDs, once each (skip the duplicate when they are the same person). The requester is the Matrix sender of the message that requested the current work — never assume the Team Admin sent it unless they actually did.

A Team Room report is mandatory after state-changing project/task actions: node status change, next wave started, project complete, blocker needing action, ambiguity needing an answer, escalation. Batch same-turn changes into one report. Report current state, outcomes, and next steps — never raw command output or coordination logs.

Leader DM is direct human conversation, not a report channel. A Leader DM message never satisfies a requester-report requirement — the concise post-delegation DM update is a transition note, not the report.

## Leader DM Project Requests

While coordination is internal (reading skills, planning, creating the project, delegating), your visible DM reply is exactly `NO_REPLY` — including tool preambles like "let me check". The first visible output is the Team Room assignment sent by `delegate-commit`, or — when assignment cannot proceed — the blocker/question itself in the DM (bridge adaptation: a DM turn cannot reach the Team Room; the human requester is in the DM).

Success path after delegation: `delegate-commit` posts the Team Room assignment; your one concise requester update goes to the DM itself (a DM turn has no other path); the formal dual-anchor Team Room report happens on the next Team-Room-triggered turn, for example a Worker TASK_COMPLETED.

## Anti-Loop

- If your message doesn't require the recipient to *do* something, don't @mention them — no thanks, confirmations, "got it", "noted", "working on it", encouragement, or status symbols.
- @mention the requester only when: (1) the delegated task is complete, (2) you hit a blocker, (3) you need a decision. @mention a Worker only for a question or a relayed decision — assignments are delivered automatically.
- If 2+ rounds of @mentions produce no new task, question, or decision, stop replying immediately.

Before any @mention, strip all Matrix IDs from the message in your head; send only if what remains carries a concrete completion, blocker, question, decision, or result.

Write human-facing text in the language selected by your AGENTS.md Response Language. In prose around a mention, use the person's display name from the Coordination block, not the bare worker ID.
