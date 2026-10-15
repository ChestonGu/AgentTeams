# worker-bridge Leader Agent 模板

worker-bridge 形态的 Team Leader 源模板。`member.role=team_leader` 的 MemberRuntimeConfig
由 `bridge/generate_agent_md.py` 自动分派到本目录（worker/standalone 走
`template/worker-bridge-agent/`），渲染出 leader 沙箱会话使用的 agent.md：builtin 围栏正文
+ team-context 围栏内 `{{COORDINATION}}`（Team Admin / Team Workers / Coordinator Members /
Team Roster，leader 视角）+ `{{ENVIRONMENT}}`，两个占位符各恰好一次。golden 基准
`bridge/tests/fixtures/golden-leader.md` 逐字节锁定渲染结果——改模板正文后必须重新生成
golden 并保持生成器测试全绿。

包含：

| 文件 | 作用 |
|---|---|
| `AGENTS.md` | leader 版 agent.md 源模板：copaw `manager/agent/team-leader-agent/AGENTS.md` 权威语义的桥化改写（MCP 工具面 → `projectflow`/`agentteams-sync` bash CLI；`taskflow(delegate_task)` 自动通知 → `delegate-commit --assignee-mxid` 自动通知；heartbeat 段与 SOUL/HEARTBEAT/memory 工作区引用删除；新增 Cross-Room Sending 桥化段） |
| `skills/task-management/SKILL.md` | leader 命令参考：`projectflow` 全动词 + 委派三步（ready → delegate → delegate-commit 自动通知，禁止二次手发）+ TASK_COMPLETED 验收流（check → 标记 `[x]` + push → 下一波） |
| `skills/communication/SKILL.md` | 路由表（当前房直答 / assignment=delegate-commit 自动 / requester 报告=Team Room @双锚 / Leader DM=人类直接对话非报告通道）+ anti-loop 三条 + DM 项目请求 NO_REPLY 规则 |
| `skills/task-management/scripts/` | 部署件 `projectflow.py` / `projectflow_core.py` / `mc_sync.py` / `agentteams_log.py`（与 `cli/projectflow/` 逐字节一致，构建时从这里取） |

部署形态：模板目录随 bridge 镜像内置（`DEFAULT_LEADER_TEMPLATE` 相对 bridge 目录解析），
leader 沙箱会话启动时 `generate_agent_md.py` 按 runtime.yaml 的 role 自动选模板渲染
agent.md，技能树随工作区下发；`projectflow` 以 `--sync mc` 读写与 copaw 同一套 MinIO
协议文件（plan.md / meta.json / spec.md / result.md），与 copaw worker 混编时字节兼容。

Leader 与 worker 的关键差异（都落在 CLI 语义里，见契约 §5.4）：

- leader 推送**不排除** `spec.md`/`plan.md`（协议所有方是 leader）；
- leader 的 `check` 读 `result.md` 全文并给出 `effective_for_acceptance`；
- worker 的 `taskflow` CLI 没有 leader 动词，`projectflow` 没有 worker 动词
  ——角色分离在工具面而非提示词面。
