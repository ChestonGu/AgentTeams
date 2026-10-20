# worker-bridge Leader Agent 模板

worker-bridge 形态的 Team Leader 模板**单一目录**（v3.0 起出站形态已删——曾经的
reply/outbound 双变体目录、skill-variants 运行期切换、`BRIDGE_SEND_URL` 分派全部
移除，指派交付只剩回复送达一种形态）。`member.role=team_leader` 的
MemberRuntimeConfig 由 `bridge/generate_agent_md.py` 选中本目录的 `AGENTS.md`，
渲染出 leader 沙箱会话使用的 agent.md（builtin 围栏正文 + team-context 围栏内
`{{COORDINATION}}`（Team Admin / Team Workers / Coordinator Members / Team
Roster，leader 视角）+ `{{ENVIRONMENT}}`，两个占位符各恰好一次）。golden 基准
`bridge/tests/fixtures/golden-leader.md` 逐字节锁定渲染结果——改模板正文后必须
重新生成 golden 并保持生成器测试全绿。

指派交付语义（回复送达，与 worker 消息同一通道）：`delegate-commit` 从不自己
发送任何东西——它记录指派状态并打印 `---- assignment text begin/end ----` 标记
块，leader 把标记块内文本原样粘进自己的会话回复；回复的开头 `@mxid` 由 bridge
回复发送路径转成结构化 mention 唤醒 assignee（与 worker 的 TASK_COMPLETED 同
机制）。

包含：

| 文件 | 作用 |
|---|---|
| `AGENTS.md` | leader 主提示词（需求澄清 Step 0 / kickoff 三步 / 委派状态机 / 凭据协议 v5 授权） |
| `skills/task-management/SKILL.md` | 委派状态机 skill 文档（projectflow 用法；回复粘贴送达规则） |
| `skills/task-management/scripts/` | 部署件 `projectflow.py` / `projectflow_core.py` / `notify.py` / `mc_sync.py` / `agentteams_log.py`（与 `cli/projectflow/` 逐字节一致的部署副本，构建时从这里取） |
| `skills/communication/SKILL.md` | 路由表（当前房直答 / assignment=delegate-commit 打印粘贴 / requester 报告=Team Room @双锚 / Leader DM=人类直接对话非报告通道）+ anti-loop 三条 + DM 项目请求 NO_REPLY 规则 |
| `skills/file-sharing/` | leader 版文件协作 skill（与 worker 树 scripts 逐字节相同、SKILL.md 带 leader 序言） |

部署形态：AGENTS.md 随 bridge 镜像内置（生成器按 `<gen>/../template/…` 相对
解析）；skills 整树随 leader 沙箱镜像装 `/opt/agentteams/skills/`（pod 形态经
`runtimeParameter.sandboxImage` 选 `opencode-runtime-leader` 变体镜像）；stale
runtime.yaml 里残留的 `bridge.runtimeParameter.bridgeSendUrl` 键被生成器忽略。
`projectflow` 以 `--sync mc` 读写与 copaw 同一套 MinIO 协议文件（plan.md /
meta.json / spec.md / result.md），与 copaw worker 混编时字节兼容。

Leader 与 worker 的关键差异（都落在 CLI 语义里，见契约 §5.4）：

- leader 推送**不排除** `spec.md`/`plan.md`（协议所有方是 leader）；
- leader 的 `check` 读 `result.md` 全文并给出 `effective_for_acceptance`；
- worker 的 `taskflow` CLI 没有 leader 动词，`projectflow` 没有 worker 动词
  ——角色分离在工具面而非提示词面。
