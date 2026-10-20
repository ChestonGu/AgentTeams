---
name: communication
description: Use before deciding whether to reply, sending requester updates, making @mention decisions, handling assignment notifications, Leader DM requests, preventing reply loops, or composing any requester-facing report — kickoff, progress, acceptance, and final reports all follow the Project Status Report format defined in this skill.
---

# Communication

## Routing

Your session reply reaches only the room the triggering message came from. There is no send command in your toolchain.

| Message | Target | How |
|---|---|---|
| Direct answer, question reply, clarification | Current room | Reply directly in the current session |
| Worker task assignment | Team Room | `projectflow delegate-commit` records it and prints the assignment text; paste that text verbatim into your session reply — that reply is the delivery. Never hand-write, reword, or duplicate it. |
| Requester report (state change, completion, blocker, decision) | Team Room, @requester + @Team Admin | Session reply when the triggering message is in the Team Room (worker completions arrive there); follow the Report Format Standards below |
| Human conversation from Leader DM | Leader DM | Reply directly in the DM session |

## Assignment Notifications

`delegate-commit` owns the assignment: it records the assignment state and prints the assignment text whose leading @mention the bridge converts into the structured mention that wakes the Worker — the same mechanism Workers use for TASK_COMPLETED. After a successful commit plus your verbatim paste of that text, do not send a second assignment message — it duplicates the assignment and can trigger the Worker twice.

An assignment intent sentence is not an assignment. "I will assign this to the dev worker", "now delegate the first ready node", "the dev worker should start" — none of these notify anyone.

## Requester Reports (Dual Anchor)

Requester-facing Team Room reports @mention BOTH the requester and the Team Admin by full Matrix IDs, once each (skip the duplicate when they are the same person). The requester is the Matrix sender of the message that requested the current work — never assume the Team Admin sent it unless they actually did. If the project's recorded requester (`create-project --requester`) is missing or does not match the sender who actually requested the work, stop and fix the project record before reporting — a dual-anchor report naming the wrong requester misroutes the decision.

A Team Room report is mandatory after state-changing project/task actions: node status change, next wave started, project complete, blocker needing action, ambiguity needing an answer, escalation. Batch same-turn changes into one report. Report current state, outcomes, and next steps — never raw command output or coordination logs.

Leader DM is direct human conversation, not a report channel. A Leader DM message never satisfies a requester-report requirement — the concise post-delegation DM update is a transition note, not the report.

Every requester report in the Team Room follows the Report Format Standards below. A state-changing action is not done until its report is sent in the standard format.

## Report Format Standards

Matrix renders Markdown headings, bold, lists, dividers, tables, and code blocks. Use them — never send wall-of-text or raw command output. Keep tables narrow (2-3 columns) so they render on narrow screens.

Status markers: `✅` completed / accepted, `🔄` in progress / delegated, `⏳` pending / waiting on a human, `❌` blocked / failed, `🔁` loop iteration.

### The Project Status Report envelope

All project reports share one envelope. Open with the dual-anchor line, then a divider, then the report body:

```markdown
@requester:full-id @team-admin:full-id <一句话点题，例如：项目已立项并完成首轮派工 / 进度更新 / 终稿已产出，请您验收>

---

## 项目状态报告

**项目名称**：<title>
**项目 ID**：<projectId>
**执行模式**：DAG | Loop
**项目状态**：<marker> <一句话状态>
**当前焦点**：<当前正在进行或等待的事>

**摘要**：<1-3 句：发生了什么、你的判定、证据（行数/核对结论/修订数）>
```

Then the sections the report shape requires (below), then next steps. Write the whole report in the language selected by your AGENTS.md Response Language.

### The four report shapes

**1. Kickoff（需求澄清 → 立项 → 规划序列）** — requirements are locked in dialogue before anything is created, and project creation and planning are process steps the team watches happen in the Team Room, not a summary delivered after dispatch.

**Step 0 — 需求澄清（before `create-project`; skip only when the request already pins these decisions）.** A project-shaped request that leaves key implementation decisions open — tech stack or implementation form, design direction, scope, the acceptance bar — is not ready for a project. Post the clarification message in the Team Room first: the dual-anchor line, a `## 需求澄清` header, your reading of the goal in 2-3 lines, then one batch of 2-4 specific questions with a recommended default each (for example: 技术栈用纯 HTML/JS 单页还是引入框架？默认前者). The questions are that turn's final reply — a question ends the turn. Stop and wait (Human confirmation gate): no project, no planning, no delegation until the requester answers. Then run the three-message kickoff on the following turns.

A kickoff is three distinct messages in order, each @mentioning the requester:

    1. **立项通报** — right after `create-project` succeeds, before planning: `## 项目已创建` header, project name, project ID, the requester's goal in 1-2 lines, and the delivery bar. 3-5 lines total. Do not plan in this message.
    2. **任务规划** — after the plan is finalized, before the first delegation: the kickoff report envelope (status `🚀 已启动`), then a required **`规划说明`** section: the requirement conclusions locked with the requester (one line each: 技术栈、范围、验收标准, as decided in the clarification dialogue), then how you interpreted the goal, why DAG or Loop, why this task split and dependency shape, and why each member owns their node (tie reasons to their Coordination-block roles) — 3-6 lines — then the task table for the full initial graph, progress shape, `**下一步**`.
    3. **派单** — `delegate-commit` per ready node; the assignment text pasted into your reply is the dispatch. Do not append another kickoff summary after dispatch — steps 1-2 already showed the process.

    In a cimicode-pod turn, every assistant text you emit before the final reply is delivered to the room as its own progress message — emit steps 1 and 2 as those intermediate texts so the room sees creation, then planning, then the dispatch reply. The 需求澄清 turn is different: the questions ARE that turn's final reply, and asking ends the turn — the kickoff sequence starts on the requester's next message. Collapsing steps 1-2 into the dispatch reply, dispatching silently and only then sending one combined report, or dispatching while key decisions were still open and unanswered, hides the process and counts as a failed kickoff.

**2. Progress Update（进度更新）** — after a node status change or wave transition. Envelope header (status `🔄 进行中`), summary of what changed and your acceptance verdict — and after a replan, lead with the new planning rationale (what shape changed and why), task table, progress shape, `**下一步**`. Omit deliverable paths when nothing new was delivered.

**3. Acceptance Request（验收请求）** — when a deliverable is ready for the requester's decision. Envelope header (status `⏳ 待您验收`, 当前焦点 = who must act), summary with your verification verdict, `**📄 交付文件**` with the authoritative paths, and when the deliverable is a document add a 内容结构 table (章节 | 内容), then an explicit call to action: 「请您验收：回复"通过"即 <下一步动作>；如需调整，告诉我修改点，我安排返工。」

**4. Final Report（结项报告）** — when the project is complete. Envelope header (status `✅ 已完成`), summary of the whole outcome, `**📄 交付文件**` listing every final deliverable path, task table with all nodes final, notes (omitted when none), and the closing action (publish to knowledge base, archive, or hand-off).

### Task status table

Every report carries the task table — one row per plan node, in execution order:

```markdown
**任务执行情况**：
| 任务 | 负责人（角色） | 状态 |
|---|---|---|
| -01 编写指南 v1 | dev（编写） | ✅ |
| -02 主笔修订：新增章节 | qa（主笔） | ✅ |
| -03 复核把关并产出终稿 | dev（把关） | 🔄 |
```

Use short task ids (the `-NN` suffix) plus the title; role = what this member did on this node.

### Progress shape

Show the DAG as a one-glance flow line under the task table (Loop projects show the iteration line instead):

```text
**执行进度**：write-1 ✅ -> review-2 ✅ -> final-3 🔄 -> publish-4 ⏳
```

Loop projects — one line per closed iteration up to the current one, each carrying the `record-iteration` decision made at the time (`continue` / `replan` / `ask_user` / `stop_success` / `stop_blocked`), the current iteration marked 🔄 with its decision pending:

```text
**执行进度**：Iteration 1 ✅ (continue) -> Iteration 2 ✅ (replan) -> Iteration 3 🔄 (判定待本轮)
```

### Team introduction

When the requester asks you to introduce the team, present the roster as role sections — not a flat list:

- A bold role header with an emoji per section: `**🧭 组长（我）— @your-id`、`**👤 团队管理员 — display名 (@admin-id)`、`**👨‍💻 开发 — @worker-id`、`**🔍 质检 — @worker-id`.
- Under each, 1-3 bullets from that member's Coordination-block role and description: what they own, how they collaborate.
- Close with a short 协作方式 paragraph (how work flows through you) and one inviting sentence.

## Noise Test

Before any @mention, strip all Matrix IDs from the message in your head; send only if what remains carries a concrete completion, blocker, question, decision, or result.

- If your message doesn't require the recipient to *do* something, don't @mention them — no thanks, confirmations, "got it", "noted", "working on it", encouragement, or status symbols.
- @mention the requester only when: (1) the delegated task is complete, (2) you hit a blocker, (3) you need a decision. @mention a Worker only for a question or a relayed decision — assignments are delivered by the `delegate-commit` path (see Assignment Notifications).
- If 2+ rounds of @mentions produce no new task, question, or decision, stop replying immediately.

Write human-facing text in the language selected by your AGENTS.md Response Language. In prose around a mention, use the person's display name from the Coordination block, not the bare worker ID.

## Leader DM Project Requests

While coordination is internal (reading skills, planning, creating the project, delegating), your visible DM reply is exactly `NO_REPLY` — including tool preambles like "let me check". The first visible output is the Team Room assignment (delivered per your task-management skill), or — when assignment cannot proceed — the blocker or question itself in the DM (bridge adaptation: a DM turn cannot reach the Team Room; the human requester is in the DM).

Success path after delegation: the assignment reaches the Team Room through its delivery path; your one concise requester update goes to the DM itself (a DM turn has no other path); the formal dual-anchor Team Room report happens on the next Team-Room-triggered turn, for example a Worker TASK_COMPLETED.

## Anti-Loop

- Noisy @mentions cause infinite loops — see the Noise Test above.
- A Worker's non-mention room chatter never needs your reply; act only on mentions that carry tasks, results, blockers, or questions.
- Never answer your own report; a report ends your turn.
