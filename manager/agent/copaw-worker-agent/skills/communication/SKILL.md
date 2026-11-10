---
name: communication
description: Use before sending @mentions, deciding whether to reply, TASK_COMPLETED, BLOCKED, QUESTION, direct answers, loop prevention, or suppressing low-information acknowledgements.
---

# Communication

## Routing

Always reply directly in the current room. Do not use the `message` tool for cross-room sends — Workers do not communicate across rooms.

## Coordinator Identity

Your coordinator is the sender of the current task assignment message. Use that Matrix ID directly for @mentions. Do not call `organization` or `agt` CLI to look up your coordinator during standard task flows.

**Direct-collaboration teams** (`collaboration: direct`): there is no task protocol and no coordinator. The member you report to is the **requester** — the member who @mentioned you with the work (the sender of the current request message). Never report to the Team Leader unless the Team Leader was the one who @mentioned you. The Team Leader is a regular member in this mode.

## @Mention Rules

Use a full Matrix ID when the recipient must act.

### Display names in prose

Every team member has a human-friendly display name — the preferred name shown first in the Coordination block / Team Workers list (for example 数据分析, 拆解主管, 压测队长1). When you write about a person in prose — greetings, tables, status reports, self-introductions, task assignments — use that display name, not the bare worker ID ("@数据分析 请开始统计" reads human; "full1-w1 请开始统计" reads as machine output). This applies to how you introduce yourself too.

Matrix IDs still belong inside @mentions (the `@name:domain` prefix that carries the notification); the text around a mention stays display-name prose.

Mention your coordinator only for:

- Task completion: `@coordinator:domain TASK_COMPLETED: <summary>`
- Blocker: `@coordinator:domain BLOCKED: <what is blocking you>`
- Question: `@coordinator:domain QUESTION: <your question>`
- Direct answer to a coordinator question

In a **direct-collaboration team** the same four cases target the **requester** instead: completion reports, blockers, questions, and answers all go to the requester's full Matrix ID in the Team Room. There is no `TASK_COMPLETED` prefix and no task-ID in these reports — they are one short message with the outcome and the shared-relative deliverable paths.

Do not @mention for:

- "Got it"
- "Thanks"
- "Working on it"
- Encouragement-only replies
- Status symbols such as green dots or check marks
- Short acknowledgments such as `ok`, `done`, `收到`, or `好的`
- Mid-task progress that requires no decision

Exception: when a new assigned task arrives, `task-management` requires you to directly say in the current room that you received the message before task acceptance work starts. Do not turn it into a progress thread, and do not send repeated acknowledgements for the same task.

Before sending any @mention, remove all Matrix IDs from the message in your head. Send only if the remaining text contains a concrete completion, blocker, question, requested answer, or decision.

## History Context

If your message includes a history section, treat it as context only. Act on the current message section.

## Loop Safeguard

If two rounds of replies produce no new task, question, or decision, stop replying.
