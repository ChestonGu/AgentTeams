# Controller / 部署侧移交说明（v2，含 v2.4 §6 生成架构）

**结论先行**：controller **零代码改动**（v1 移交的 `builtinAgentDir()`
加 case 一项已取消；v2.4 后 canonical AGENTS.md 的合成对 opencode 更是
彻底无关）。

## 1. 架构如何消费 controller 数据（v2 → v2.4 演变）

| 项 | v1（走 controller/MinIO） | v2.4（去向） |
|---|---|---|
| skills + CLI 脚本 | deployer 分发到 `agents/<name>/skills/` | **镜像预装**（部署侧构建） |
| AGENTS.md | 部署时渲染存 MinIO、沙箱拉取 | **bridge 每次会话调 `bridge/generate_agent_md.py`：源模板（镜像内）+ runtime.yaml 渲染出 agent.md 作 system prompt**——canonical AGENTS.md 与 opencode 无关，不拉取不解析 |
| SOUL.md / PROFILE.md | MinIO 同步进沙箱 | **内容合并**：bridge 拉取后经 `--soul-file`/`--profile-file` 并入 prompt（前置 `## Persona` 接缝段；文件本身不进沙箱） |
| memory / HEARTBEAT | MinIO 同步 | **取消**（沙箱无状态） |

## 2. 部署侧剩余事项（非 controller 代码）

1. **显式部署配方（两条约束的组合，缺一不可）**：

   ```yaml
   # Worker CR —— runtime 字段保持 qwenpaw（或 Member deployMode: edge），
   # 镜像用 spec.image 覆盖成 opencode 沙箱镜像
   spec:
     runtime: qwenpaw                # 别填新值：runtime.yaml 只在
                                     # qwenpaw/edge 调谐分支写入
                                     # （member_reconcile.go:454）
     image: <opencode-sandbox:tag>   # 镜像优先级最高：spec.image >
                                     # per-runtime 配置 > 全局 WorkerImage
                                     # （backend/kubernetes.go），
                                     # controller 零改动
   ```

   直觉做法 `runtime: opencode` 会**不落 runtime.yaml 写入分支** →
   §6 生成器无输入（fail-loud 兜住、bridge 拒开会话，但排障困惑）。
   生产 worker 真件已确认同型部署（qwenpaw/edge 分支）。
2. **镜像构建（两个目标）**：worker 沙箱镜像预装清单见契约 §2
   （mc / python3 / jq / skills 集 + scripts / PATH wrapper）；
   **bridge 侧**（daemon 容器）预装生成器 + 源模板 + PyYAML（契约 §2
   bridge 段——生成器不进沙箱镜像）。镜像内 skills 文件与仓库
   `opencode/template/opencode-worker-agent/skills/` 逐字节一致，构建时
   从本仓库取。

## 3. controller 保留的数据义务

- **runtime.yaml**（`agents/<name>/runtime/runtime.yaml`）：身份 + 团队
  事实的**唯一来源**（member 身份、team.name/admin/members 名册、
  storage 前缀）。controller 现有写入路径**照常不动**；团队变化经
  `MergeMemberRuntimeTeamContext` 更新 team 段，opencode worker 下次
  会话自动拿到新名册。**bridge 无需向 controller 查询任何名册**。
- **persona 文件**：MinIO `agents/<name>/SOUL.md`（存在时连同
  `PROFILE.md`）由 bridge 拉取。controller 现有 SOUL.md 生成/注入路径
  **照常不动**（copaw 与 opencode 共用同一份 MinIO 数据）；PROFILE.md
  当前无生成方，存在即生效（copaw 同语义）。
- canonical AGENTS.md / `## Coordination` 块注入：**照常不动**——
  copaw leader/worker 继续消费；opencode 侧由生成器从 runtime.yaml
  渲染等价信息（文案与 `agentconfig/coordination.go` 逐行对齐）。

## 4. 明确不需要的部分（及理由）

| 项 | 理由 |
|---|---|
| openclaw.json 生成管线 | opencode worker 不消费；copaw worker 照常，不动 |
| canonical AGENTS.md 合成（对 opencode） | v2.4 起 opencode 不拉取不解析；qwenpaw/edge 分支本就不跑该合成 |
| SOUL.md.tmpl / HEARTBEAT 注入 | 沙箱无状态不落文件；persona 经生成器进 prompt（SOUL.md 在 MinIO 照常生成）；team_leader（copaw）路径不动 |
| MinIO 布局 / 任务协议 | opencode worker 与 copaw leader 经 `shared/tasks/{id}/` 字节互通，controller 无感 |
| Worker CR schema | `spec.runtime` / `spec.image` 字段均已存在 |

## 5. 验收钩子（部署侧自检）

- 镜像内抽查 `<skills-root>/task-management/scripts/taskflow.py` 与
  `opencode/cli/taskflow/taskflow.py` 逐字节一致
  （`verify/simulator.py` 已内置全部 12 个部署副本的漂移检查）。
- worker pod 起来后：`taskflow check <任意已分配任务>` 能跑（env 齐全、
  alias 三模式其一生效）。
- bridge 下发的 prompt 为 `bridge/generate_agent_md.py` 的输出：以
  `<!-- agentteams-builtin-start -->` 开头（标记骨架完整）、含
  `## Coordination`（渲染自 runtime.yaml：Coordinator = team_leader
  成员、Team Admin、Coordinator Members）与 Environment 段、不含
  find-skills / QwenPaw 字样、无 `{{` 残留；配了 SOUL.md 时文末有
  `## Persona` 接缝 + persona 段落。
- 生成器 fail-loud 抽查：删掉 runtime.yaml 里 `member.name` 重跑 →
  退出码 1 且 stderr 有 `member.name`（bridge 据此拒开会话）。
- **统一日志**（契约 §5.5）：跑一轮任务后
  `$AGENTTEAMS_FS_ROOT/logs/agentteams.log` 存在且为 JSONL，同文件内
  既有 `tool: taskflow` 也有（leader 同 pod 时）`tool: projectflow`
  记录；每条命令首尾是 `cmd_start`/`cmd_end`。生成器日志在 **bridge
  侧日志文件**（调用方经 `AGENTTEAMS_LOG_FILE` 指定，与沙箱日志分属
  两个进程/容器，见契约 §2 bridge 段）：`tool: generate_agent_md` 的
  `generated` 事件在场即可。

---

## 附：v1 移交内容（已被本版取代，留档）

<details>
<summary>v1：builtinAgentDir() 加 opencode case（已取消）</summary>

文件：`agentteams-controller/internal/service/deployer.go:1488`

```go
case "opencode": // 与 copaw/hermes case 同型
    return filepath.Join(baseDir, "opencode-worker-agent")
```

取消原因：v2 下模板不经 controller/MinIO 分发（见上表）。若未来架构回退
到 per-agent 模板分发，此改动仍有效。
</details>
