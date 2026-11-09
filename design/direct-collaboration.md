# Direct 协作模式（spec.collaboration=direct）设计与实施

> 状态：代码实施完成（本文件即改动清单与验证方案）。读者：要维护/部署/排障此模式的工程师。
> 前置概念：现有 taskflow 协作机制见 `design/status-event-reporting-spec.md`；direct 事件契约见该文档 §4A。

## 1. 模式语义

Team CR 新增字段：

```yaml
spec:
  collaboration: direct   # taskflow（默认/空）| direct
```

- **taskflow（默认）**：现状不变——leader 规划 project/DAG，worker 走 ack→submit→TASK_COMPLETED。
- **direct**：队内无任务协议。人类在 Team Room 直接 @worker（或 leader）派活，被 @ 者自己干活，交付物写 `shared/outputs/<worker>/` 推送共享存储，完成回执 @requester 附共享相对路径；状态经 `direct_work_started/completed` 事件上报。
- 两模式**按队共存**，互不影响；同队可切换（切换语义见 §5.3）。leader 保留并转 direct：可被 @ 干活 + 媒体归档/房成员协调两个专属职责。

**硬摘除原则**：direct 不是"模板不提任务"，而是任务工具从 agent 视野中移除——copaw 不注册 taskflow/projectflow 工具、bridge 沙箱不加载 task-management skill 且 taskflow wrapper 停用。

## 2. 全链路

```
Team CR spec.collaboration
  │
  ├─ controller（agentteams-controller）
  │   ├─ runtime.yaml team.collaboration 投影（runtime_config.go）
  │   ├─ AGENTS 底版选择 AGENTS-direct.md（prepareAndPushAgentsMD）
  │   ├─ 协作块 direct 变体（agentconfig/coordination.go）
  │   ├─ skills 门控 + 互斥清理（pushBuiltinSkills）
  │   └─ leader SOUL-direct.md.tmpl / HEARTBEAT-direct.md 变体
  │
  ├─ copaw worker/leader pod
  │   └─ hooks/__init__.py _collaboration_mode() 读 runtime.yaml
  │       direct → 注册 work_report，不注册 taskflow/projectflow
  │
  ├─ bridge pod（worker-bridge runtime）
  │   └─ generate_agent_md.py 读 runtime.yaml → 选 AGENTS-direct.md 模板
  │       + direct 协作块渲染（每 turn 现渲染，无额外生效步骤）
  │
  └─ sandbox pod（opencode/cimicode-runtime）
      ├─ operator 从 Team CR 读 collaboration → env AGENTTEAMS_COLLABORATION
      └─ entrypoint：direct → skills.paths 排除 task-management
          + taskflow wrapper mv 停用（可逆）；切回自动恢复
```

## 3. 改动文件清单

### controller（镜像：agentteams-controller）

| 文件 | 改动 |
|---|---|
| `api/v1beta1/types.go` | TeamSpec 加 `Collaboration`（enum taskflow;direct;""）+ `CollaborationDirect` 常量 + `EffectiveCollaboration()` |
| `config/crd/teams.agentteams.io.yaml` | 同步加 `collaboration` 属性 |
| `internal/server/types.go` | Create/UpdateTeamRequest、TeamResponse 加 `collaboration` |
| `internal/server/resource_handler.go` | Create/Update 透传 + 值校验；teamToResponse 填 EffectiveCollaboration |
| `internal/service/runtime_config.go` | `memberRuntimeConfigTeam.Collaboration` + `applyRuntimeTeamContext` 填充 |
| `internal/service/deployer.go` | 4 个 request struct 加 Collaboration；`prepareAndPushAgentsMD`/`pushBuiltinSkills`/`pushBuiltinTopLevelFiles` 加 collaboration 形参；direct 底版选择；skills 门控 + DeletePrefix 互斥清理；HEARTBEAT 变体重投影；`renderAndPushSoulTemplate` 选 SOUL-direct |
| `internal/agentconfig/coordination.go` | `CoordinationContext.Collaboration`；worker/team_leader direct 协作块（coordinator 锚 → requester 锚） |
| `internal/controller/team_controller.go` | SyncTeamLeaderAssets/InjectCoordinationContext/InjectWorkerCoordination/deployTeamRuntimeConfigs 四处填 Collaboration |
| `cmd/agt/create.go` | `agt create team --collaboration` |

### copaw（镜像：copaw-worker）

| 文件 | 改动 |
|---|---|
| `copaw/src/copaw_worker/hooks/__init__.py` | `_collaboration_mode()`（读 runtime.yaml，无文件/异常→""=taskflow）；注册处分叉：direct→仅 work_report，否则 taskflow+projectflow（原样） |
| `copaw/src/copaw_worker/hooks/tools/work_report.py` | 新工具：action=started/completed，summary/requester/deliverables/status 校验，构造 direct_work_* 事件，先 pull_shared_config 再 emit_event |
| `copaw/src/copaw_worker/hooks/tools/status_report.py` | 新增公共 `emit_event()`（日志+webhook 双写）；`report_tool_event` 复用，行为不变 |
| `copaw/tests/test_work_report.py` | 16 用例（信封/校验/config pull/emit_event 通道/mode 解析） |

### 模板资产（controller 镜像 `/opt/agentteams/agent/`，Makefile 构建前 `cp -r manager/agent`）

| 文件 | 用途 |
|---|---|
| `manager/agent/copaw-worker-agent/AGENTS-direct.md` | direct worker 底版 |
| `manager/agent/copaw-worker-agent/skills/direct-work/SKILL.md` | direct-work skill（目录约定/push/work_report/回执/凭据） |
| `manager/agent/team-leader-agent/AGENTS-direct.md` | direct leader 底版（含 Leader DM 定位段、媒体归档职责） |
| `manager/agent/team-leader-agent/skills/direct-work/SKILL.md` | leader direct-work（+归档段） |
| `manager/agent/team-leader-agent/SOUL-direct.md.tmpl` | direct leader 人格（无拆分/委派） |
| `manager/agent/team-leader-agent/HEARTBEAT-direct.md` | direct 心跳（无 check_task；无事项不发声） |

### bridge（镜像：worker-bridge + opencode/cimicode-runtime + operator）

| 文件 | 改动 |
|---|---|
| `worker-bridge/bridge/generate_agent_md.py` | parse 返回 `collaboration`；`DIRECT_TEMPLATE` 常量；main 未传 --template 时按 mode 选模板；render_coordination direct 变体 |
| `worker-bridge/bridge/Dockerfile` | COPY AGENTS-direct.md 到 /opt/template（保持生成器相对路径配对） |
| `worker-bridge/template/worker-bridge-agent/AGENTS-direct.md` | direct worker 底版（{{COORDINATION}}/{{ENVIRONMENT}} 骨架不变） |
| `worker-bridge/template/worker-bridge-agent/skills/direct-work/` | SKILL.md + scripts/work_report.py + vendored status_report.py/mc_sync.py + tests/test_work_report.py（7 用例） |
| `worker-bridge/cli/{taskflow,projectflow}/status_report.py` + 两份 template task-management 拷贝 | 新增 `emit_event()`，五份 vendored sha256 同步（direct-work 为第五份） |
| `worker-bridge/operator/worker_bridge_operator.py` | `team_spec_for()`；sandbox env 注入 `AGENTTEAMS_COLLABORATION` |
| `worker-bridge/opencode-runtime/Dockerfile`、`cimicode-runtime/Dockerfile` | 新增 `work-report` wrapper（→ direct-work/scripts/work_report.py） |
| `worker-bridge/opencode-runtime/entrypoint.sh`、`cimicode-runtime/entrypoint.sh` | direct 时重写 skills.paths（排除 task-management）+ taskflow wrapper mv 停用；切回恢复（标记文件 `$CFG_DIR/.agentteams-direct`） |
| `worker-bridge/bridge/tests/{fixtures/runtime-direct.yaml,fixtures/golden-direct.md,test_generate_agent_md.py}` | direct golden + 4 新用例；taskflow/standalone golden 零变化 |

### 文档

- `design/status-event-reporting-spec.md` §4A：direct 事件契约（work 块、示例、接收方注意）
- `changelog/current.md`：feat(collaboration) 条目

## 4. 用法

### 建 direct 队

```bash
# API
curl -X POST $CONTROLLER/api/v1/teams -H "Authorization: Bearer $TOKEN" -d '{
  "name":"t-dc1","displayName":"Direct Team 1","collaboration":"direct",
  "admin":{"name":"admin"},"humanMembers":[{"name":"admin"}],
  "workerMembers":[{"name":"dc1-lead","role":"team_leader"},
                   {"name":"dc1-dev","role":"worker"}]}'

# 或 agt CLI（镜像内）
agt create team --name t-dc1 --leader-name dc1-lead --workers dc1-dev --collaboration direct
```

生效面（建队后应看到）：
- MinIO `agents/<w>/AGENTS.md` = direct 底版 + direct 协作块（含 "collaboration: direct"）
- `agents/<w>/skills/` 无 task-management（leader 另无 project-management/team-coordination），有 direct-work
- `agents/<w>/runtime/runtime.yaml` 的 `team.collaboration: direct`
- copaw 工具列表无 taskflow/projectflow、有 work_report（每 turn 新 toolkit 按 runtime.yaml 判定）
- bridge 沙箱 `opencode.json`/`cimicode.json` skills.paths 逐 skill 且无 task-management；`taskflow` 命令不存在，`work-report` 存在

### 工作方式（agent 视角）

被 @ → 简短应答 →（长任务 work_report started）→ 干活 → 写 `shared/outputs/<worker>/...` → `filesync push` / `agentteams-sync push` → work_report completed → Team Room @requester 回执附路径。

### 状态上报对接

与任务事件完全同构：env `AGENTTEAMS_STATUS_WEBHOOK_URL`/`_TOKEN` 或 `teams/<t>/shared/config/webhook.json`（per-call 拉取）。新事件值 `direct_work_started`/`direct_work_completed`，`work.deliverables[]` 是共享相对路径，接收方顺 `teams/<team>/shared/` 取文件。详见 spec §4A。

## 5. 坑与边界（实施时明确处理的点）

1. **standalone 不受影响**：无 runtime.yaml / 无 team 段 → `_collaboration_mode()=""` → taskflow 工具集原样；`golden-standalone.md` 未变。
2. **模式切换残留**：
   - skills：`pushBuiltinSkills` 先 DeletePrefix 互斥组（direct↔taskflow 双向），再按新模式推送——切换后旧协议 skill 不残留。
   - AGENTS.md：每次 reconcile 按当前模式重选底版重投影（merge builtin 段），无残留。
   - HEARTBEAT：原 seed-only 语义扩展为"存量内容等于任一 builtin 变体时随模式重投影；用户手改过的保留"。
   - SOUL：direct 时选 SOUL-direct.md.tmpl（spec.soul 显式设置时优先，不变）。
   - 沙箱：entrypoint 每次启动按 env 重算；taskflow wrapper mv 可逆；`AGENTTEAMS_COLLABORATION` env 变化依赖 operator reconcile 更新 Deployment（pod 滚动后生效）。
3. **direct 不进 dashboard 项目看板**：不写 `shared/projects/`；看板消费走 webhook/日志（spec §4A 已告知接收方）。
4. **`direct_work_started` 可缺席**：靠 agent 自觉调用，接收方不得当作 completed 的必需前置；completed 在"有交付物或最终结论"时必发（模板强制）。
5. **skill hub 分发**（内网）：direct 队技能集=通用 skills + direct-work（不含 task-management/project-management/team-coordination）；hub 覆写 `/opt/agentteams/skills` 同路径的约束不变；`work-report` wrapper 指向镜像内 direct-work 路径，hub 须携带该 skill 的 scripts 全套（work_report.py + status_report.py + mc_sync.py）。
6. **共享写冲突**：`mc mirror --overwrite` 同名覆盖无合并——per-worker 目录隔离是硬约定，模板/skill 双层禁止写他人 outputs 目录。
7. **凭据协议锚点变化**：direct 模式报告对象=requester（@ 你的人），不再是 Team Leader；静默纪律（路径/错误码/token 零泄露）原样保留；凭据等待不报 BLOCKED。
8. **golden-direct.md 是生成器产物**：手工修改 AGENTS-direct.md 或 direct 协作块渲染后必须重新生成（`gen.generate(cfg_direct, AGENTS-direct.md, date="2026-09-03")`），test_direct_golden 字节级锁定。
9. **bridge leader（worker-bridge-leader-agent）**：generate_agent_md 的 role 白名单仍仅 worker/standalone（team_leader 拒绝渲染）——bridge 线 leader 模板不在生效链（与既有状态一致）；direct leader 生效面=copaw team-leader-agent。
10. **copaw hooks 每 turn 判定**：`_collaboration_mode()` 在每个新 toolkit 创建时读一次 runtime.yaml——模式切换无需重启 copaw 进程（runtime.yaml 由 sync_loop 周期刷新）。

## 6. 105 验证场景（全量）

| # | 场景 | 验收点 |
|---|---|---|
| S1 | direct 队创建（copaw leader + copaw worker + bridge worker） | §4 生效面全中（AGENTS/skills/runtime.yaml/沙箱 paths/wrapper） |
| S2 | @copaw worker 派活 | 直接干活无 taskflow 调用；`teams/<t>/shared/outputs/<w>/` 文件落桶；Team Room @requester 回执附路径 |
| S3 | @bridge worker 派活 | 同 S2（sandbox 内 work-report + agentteams-sync 路径） |
| S4 | @copaw leader 派活 | leader 自己干活不委派；交付物/回执同上；不产生 projectflow |
| S5 | webhook sink | 投放 `shared/config/webhook.json` 后，S2-S4 的 direct_work_started/completed 到 sink，work.deliverables 路径正确，Bearer 头正确 |
| S6 | 纯 Q&A @worker | 直接回答，无 work_report 调用（无事件） |
| S7 | 媒体归档 | 群内发文件 → copaw leader 归档 `shared/knowledge/matrix/` 仍然工作 |
| S8 | 凭据协议 | 模拟 401 → 协议句发 Team Room @requester（非 leader），无路径/错误码泄露，不报 BLOCKED |
| S9 | 双 worker 并行 | 两 worker 同时被 @ 干活，各自 outputs 目录无互相覆盖 |
| S10 | taskflow 队回归 | 不填 collaboration 建队：AGENTS/skills/工具集与现状一致；create_project→delegate→ack→submit→TASK_COMPLETED 全链不变；taskflow 事件照常 |
| S11 | 模式切换 | taskflow 队改 direct：AGENTS/skills/沙箱按 direct 重投影，task-management 消失、taskflow wrapper 停用；再切回恢复 |
| S12 | standalone 回归 | standalone worker 工具集/行为不变 |

部署链：controller 镜像（CR+投影+模板树）→ copaw-worker 镜像（hooks+work_report）→ worker-bridge 镜像（生成器+direct 模板）→ opencode/cimicode-runtime 镜像（entrypoint+wrapper）→ operator 镜像（env 注入）。
