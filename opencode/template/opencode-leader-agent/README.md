# opencode Leader Agent 模板（starter，未部署）

为"后续把 Leader 运行时也换成 opencode"预置的内容。当前 copaw Leader
仍是权威实现；本目录只是**起点参考**，不进入 worker 镜像、不经
controller/MinIO 分发。

包含：

| 文件 | 作用 |
|---|---|
| `AGENTS.md` | leader 形态 agent.md 参考。**注意：`bridge/convert_agent_md.py` 目前只内置 worker 段落库，leader 转换库待 leader 迁移立项时再加**；届时按 v2.3 同一模式扩展（leader 版 §4/§5/§6/§8 段落 + Coordination 块透传 + runtime.yaml 环境段 + golden 测试；团队事实走 Coordination 块，不渲染名册表） |
| `skills/task-management/SKILL.md` | leader 命令参考：`projectflow`（项目/计划/委派/闭环）+ `taskflow check`（读 worker 结果） |
| `skills/task-management/scripts/` | 部署三件套 `projectflow.py` / `projectflow_core.py` / `mc_sync.py`（与 `cli/projectflow/` 逐字节一致，构建时从这里取） |

Leader 与 worker 的关键差异（都落在 CLI 语义里，见契约 §5）：

- leader 推送**不排除** `spec.md`/`base/`（协议所有方是 leader）；
- leader 的 `check` 读 `result.md` 全文并给出 `effective_for_acceptance`；
- worker 的 `taskflow` CLI 没有 leader 动词，`projectflow` 没有 worker 动词
  ——角色分离在工具面而非提示词面。
