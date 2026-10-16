# worker-bridge Leader Agent 模板（共享底座）

worker-bridge 形态的 Team Leader 模板**共享底座**。v2.9 起 leader 按出站方式拆成两个
形态变体目录（AGENTS.md + task-management SKILL.md 各一份，见下表）；本目录只保留
skills 整树——scripts 是 CLI 正式安装位（与 `cli/projectflow/` 逐字节一致的部署副本），
**单一来源不随变体复制**。`member.role=team_leader` 的 MemberRuntimeConfig 由
`bridge/generate_agent_md.py` 三路选型：按 runtime.yaml
`bridge.runtimeParameter.bridgeSendUrl` 有无在两个变体模板间选（有=出站/自动通知，
无=回复送达，无出站为 flagless 默认），渲染出 leader 沙箱会话使用的 agent.md
（builtin 围栏正文 + team-context 围栏内 `{{COORDINATION}}`（Team Admin / Team Workers /
Coordinator Members / Team Roster，leader 视角）+ `{{ENVIRONMENT}}`，两个占位符各恰好
一次）。golden 基准 `bridge/tests/fixtures/golden-leader-reply.md` /
`golden-leader-outbound.md` 逐字节锁定两形态渲染结果——改模板正文后必须重新生成
golden 并保持生成器测试全绿。

包含：

| 文件 | 作用 |
|---|---|
| `skills/task-management/scripts/` | 部署件 `projectflow.py` / `projectflow_core.py` / `notify.py` / `mc_sync.py` / `agentteams_log.py`（与 `cli/projectflow/` 逐字节一致，构建时从这里取；两形态共享——delegate-commit 运行期按 `BRIDGE_SEND_URL` 有无自分岔，同一 CLI 服务两形态） |
| `skills/communication/SKILL.md` | 路由表（当前房直答 / assignment=delegate-commit / requester 报告=Team Room @双锚 / Leader DM=人类直接对话非报告通道）+ anti-loop 三条 + DM 项目请求 NO_REPLY 规则（两形态共享） |
| `skills/file-sharing/` | leader 版文件协作 skill（与 worker 树 scripts 逐字节相同、SKILL.md 带 leader 序言；两形态共享） |

形态变体（AGENTS.md 与 task-management SKILL.md 各自整份，互不引用对方形态文案——
reply 版不出现 auto-notify，outbound 版不出现 reply-delivery 与粘贴标记）：

| 目录 | 形态 | 分派信号 |
|---|---|---|
| `../worker-bridge-leader-reply-agent/` | 回复送达（测试优先，flagless 默认）：`delegate-commit` 无通知端点，打印 `---- assignment text begin/end ----` 标记块，leader 会话回复原样粘贴送达 | runtime.yaml 无 `bridge.runtimeParameter.bridgeSendUrl` |
| `../worker-bridge-leader-outbound-agent/` | 出站（自动通知）：`delegate-commit` 经 `BRIDGE_SEND_URL` 自动发 Team Room 通知 | runtime.yaml 有 `bridge.runtimeParameter.bridgeSendUrl` |

部署形态：两个变体 AGENTS.md 随 bridge 镜像内置（生成器按 `<gen>/../template/…`
相对解析），SKILL.md 变体随 leader 沙箱镜像并装 `/opt/agentteams/skill-variants/
{reply,outbound}/`，entrypoint 启动时按 `BRIDGE_SEND_URL` 有无选一落位正式路径
（同一镜像双形态）；`projectflow` 以 `--sync mc` 读写与 copaw 同一套 MinIO
协议文件（plan.md / meta.json / spec.md / result.md），与 copaw worker 混编时字节兼容。

Leader 与 worker 的关键差异（都落在 CLI 语义里，见契约 §5.4）：

- leader 推送**不排除** `spec.md`/`plan.md`（协议所有方是 leader）；
- leader 的 `check` 读 `result.md` 全文并给出 `effective_for_acceptance`；
- worker 的 `taskflow` CLI 没有 leader 动词，`projectflow` 没有 worker 动词
  ——角色分离在工具面而非提示词面。
