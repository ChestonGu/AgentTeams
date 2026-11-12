# Direct 协作模式（spec.collaboration=direct）设计与实施

> 状态：设计与实施均已落地（含目录拆分与 Peer Mentions 规则），105 全场景验证通过。读者：要维护/部署/排障此模式的工程师。
> 相关文档：`design/status-event-reporting-spec.md`（§4A direct 事件契约）、`design/direct-collaboration.md`（本文件）。

## 1. 模式语义

Team CR 新增字段：

```yaml
spec:
  collaboration: direct   # taskflow（默认/空）| direct
```

- **taskflow（默认）**：现状不变——leader 规划 project/DAG，worker 走 ack→submit→TASK_COMPLETED。
- **direct**：队内无任务协议。人类在 Team Room 直接 @worker（或 leader）派活，被 @ 者自己干活，交付物写 `shared/outputs/<worker>/` 推送共享存储，完成回执 @requester 附共享相对路径；状态经 `direct_work_started/completed` 事件上报（**上报不列为本期验收项**）。
- 两模式**按队共存**，互不影响；同队可切换（切换语义见 §5.3）。leader 保留并转 direct：可被 @ 干活 + 媒体归档/房成员协调两个专属职责。

**硬摘除原则**：direct 不是"模板不提任务"，而是任务工具从 agent 视野中移除——copaw 不注册 taskflow/projectflow 工具、bridge 沙箱不加载 task-management skill 且 taskflow wrapper 停用。

## 2. 目录结构（专属隔离 + 通用 skill 共享）

两种模式的专属文件在**独立目录**，通用 skill **不复制**（推送时从 taskflow 目录取子集，单份零漂移）：

```
manager/agent/
├── copaw-worker-agent/            【taskflow】AGENTS.md
│   └── skills/{communication,file-sharing,find-skills,mcporter,organization,task-management}
├── copaw-worker-agent-direct/     【direct】AGENTS.md（含 Peer Mentions 段）
│   └── skills/direct-work/
├── team-leader-agent/             【taskflow】AGENTS.md, SOUL.md.tmpl, HEARTBEAT.md
│   └── skills/{communication,file-sharing,mcporter,organization,task-management,project-management,team-coordination}
└── team-leader-agent-direct/      【direct】AGENTS.md, SOUL.md.tmpl, HEARTBEAT.md
    └── skills/direct-work/

worker-bridge/template/
├── worker-bridge-agent/           【taskflow】AGENTS.md
│   └── skills/{communication,file-sharing,mcporter,organization,task-management}
├── worker-bridge-agent-direct/    【direct】AGENTS.md（含 Peer Mentions 段）
│   └── skills/direct-work/{SKILL.md,scripts/{work_report.py,status_report.py,mc_sync.py},tests/}
└── worker-bridge-leader-agent/    （不在生效链，不动）
```

沙箱镜像内对应为 `/opt/agentteams/skills-taskflow/` 与 `/opt/agentteams/skills-direct/` 两个目录。

**communication skill**：两模式共用单份（含 direct 段：requester 锚 vs taskflow 的 coordinator 路由），各自适用，不冲突。

## 3. 全链路

```
Team CR spec.collaboration
  ├─ controller
  │   ├─ runtime.yaml team.collaboration 投影（copaw hooks / bridge 生成器读取）
  │   ├─ builtinAgentDirFor(role, runtime, collaboration) 按模式选目录
  │   ├─ AGENTS 底版 = 模式目录/AGENTS.md（leader 与成员 worker 都重投影）
  │   ├─ 协作块 direct 变体（coordinator 锚 → requester 锚）
  │   ├─ skills 推送 = 模式目录全部 + taskflow 目录通用子集（互斥 DeletePrefix 清理）
  │   └─ leader SOUL.md.tmpl / HEARTBEAT.md 从模式目录取
  ├─ copaw worker/leader pod
  │   └─ hooks 每 turn 读 runtime.yaml：direct → 注册 work_report，不注册 taskflow/projectflow
  ├─ bridge pod
  │   └─ generate_agent_md.py 按 collaboration 选 DIRECT_TEMPLATE（worker-bridge-agent-direct/AGENTS.md）
  └─ sandbox pod（opencode/cimicode-runtime）
      ├─ operator 从 Team CR 读 collaboration → env AGENTTEAMS_COLLABORATION
      └─ entrypoint：direct → skills.paths = [skills-direct + taskflow 目录通用子集（排除 task-management）]
          + taskflow wrapper mv 停用（可逆）；taskflow → paths=[skills-taskflow]
```

## 4. 改动文件清单（最终形态）

### controller（镜像 agentteams-controller）

| 文件 | 改动 |
|---|---|
| `api/v1beta1/types.go` | TeamSpec 加 `Collaboration`（enum）+ `CollaborationDirect` 常量 + `EffectiveCollaboration()` |
| `config/crd/teams.agentteams.io.yaml` | 同步加 `collaboration` 属性（**部署必须 apply，否则字段被 K8s pruning 丢弃**） |
| `internal/server/types.go`、`resource_handler.go` | Create/Update 透传+校验；TeamResponse 填有效模式 |
| `internal/service/runtime_config.go` | `memberRuntimeConfigTeam.Collaboration` + `applyRuntimeTeamContext` 填充 |
| `internal/service/deployer.go` | `builtinAgentDirFor()` 按模式选目录；`prepareAndPushAgentsMD` 从模式目录读 AGENTS.md；`pushBuiltinSkills` 双来源（模式目录全部 + taskflow 目录通用子集）+ DeletePrefix 互斥清理；`pushBuiltinTopLevelFiles` HEARTBEAT 从模式目录取（存量为任一 builtin 变体时随模式重投影，用户手改保留）；`renderAndPushSoulTemplate` 从模式目录取 SOUL.md.tmpl |
| `internal/agentconfig/coordination.go` | `CoordinationContext.Collaboration`；worker/team_leader direct 协作块（coordinator 锚 → requester 锚），taskflow 渲染原样保留 |
| `internal/controller/team_controller.go` | SyncTeamLeaderAssets/InjectCoordinationContext/InjectWorkerCoordination/deployTeamRuntimeConfigs 填 Collaboration；detach 传 Runtime |
| `internal/controller/member_reconcile.go` | WorkerReconciler 按所属 Team 解析 collaboration + team_leader 名并传入 prepare（修 reconciler 互刷与 @manager 锚） |
| `cmd/agt/create.go` | `agt create team --collaboration` |

### copaw（镜像 copaw-worker）

| 文件 | 改动 |
|---|---|
| `hooks/__init__.py` | `_collaboration_mode()`（读 runtime.yaml，无文件/异常→""=taskflow）；direct→仅注册 work_report，否则 taskflow+projectflow（原样） |
| `hooks/tools/work_report.py` | 新工具：started/completed，summary/requester/deliverables/status 校验，direct_work_* 事件，per-call config pull |
| `hooks/tools/status_report.py` | 公共 `emit_event()`（日志+webhook 双写）；`report_tool_event` 复用，行为不变 |
| `copaw/tests/test_work_report.py` | 16 用例（信封/校验/config pull/emit_event 通道/mode 解析） |

### 模板资产（controller 镜像 `/opt/agentteams/agent/`）

| 文件 | 用途 |
|---|---|
| `copaw-worker-agent-direct/AGENTS.md` | direct worker 底版（含 Peer Mentions 段） |
| `copaw-worker-agent-direct/skills/direct-work/SKILL.md` | direct-work skill（目录约定/push/work_report/回执/凭据锚 requester） |
| `team-leader-agent-direct/AGENTS.md` | direct leader 底版（含 Leader DM 定位、媒体归档职责、Peer Mentions 段） |
| `team-leader-agent-direct/SOUL.md.tmpl` | direct leader 人格（无拆分/委派） |
| `team-leader-agent-direct/HEARTBEAT.md` | direct 心跳（无 check_task；无事不发声） |
| `team-leader-agent-direct/skills/direct-work/SKILL.md` | leader direct-work（+归档段） |
| `worker-bridge/template/worker-bridge-agent-direct/AGENTS.md` | bridge worker direct 底版（含 Peer Mentions 段） |
| `worker-bridge/template/worker-bridge-agent-direct/skills/direct-work/` | bridge direct-work（SKILL + scripts + 测试） |

### bridge（镜像 worker-bridge + opencode/cimicode-runtime + operator）

| 文件 | 改动 |
|---|---|
| `worker-bridge/bridge/generate_agent_md.py` | parse 返回 `collaboration`；`DIRECT_TEMPLATE` 指向 `worker-bridge-agent-direct/AGENTS.md`；render_coordination direct 变体；main 按 mode 选模板 |
| `worker-bridge/bridge/Dockerfile` | COPY `worker-bridge-agent-direct/AGENTS.md`（生成器相对路径配对） |
| `worker-bridge/bridge/tests/{fixtures/runtime-direct.yaml,fixtures/golden-direct.md,test_generate_agent_md.py}` | direct golden + 4 新用例；三份 golden 与当前模板同步（协作块与渲染结构不变） |
| `worker-bridge/cli/{taskflow,projectflow}/status_report.py` + 两份 template task-management 拷贝 | 新增 `emit_event()`，五份 vendored sha256 同步 |
| `worker-bridge/operator/worker_bridge_operator.py` | `team_spec_for()`；sandbox env 注入 `AGENTTEAMS_COLLABORATION` |
| `worker-bridge/opencode-runtime/Dockerfile`、`cimicode-runtime/Dockerfile` | skills 按模式分目录（skills-taskflow/skills-direct）；`work-report` wrapper |
| `worker-bridge/opencode-runtime/entrypoint.sh`、`cimicode-runtime/entrypoint.sh` | direct：paths=direct 目录 + taskflow 目录通用子集（排除 task-management）+ taskflow wrapper mv 停用；切回自动恢复（标记文件） |

### 文档

- `design/status-event-reporting-spec.md` §4A：direct 事件契约（work 块、示例、接收方注意）
- `design/direct-collaboration.md`：本文件（代码级设计/实施）
- `changelog/current.md`：feat(collaboration) 主条目 + 目录拆分与 Peer Mentions 条目 + 各修复条目

## 5. 用法

### 建 direct 队

```bash
# API
curl -X POST $CONTROLLER/api/v1/teams -H "Authorization: Bearer $TOKEN" -d '{
  "name":"t-dc1","displayName":"DC1 Direct Team","collaboration":"direct",
  "admin":{"name":"admin"},"humanMembers":[{"name":"admin"}],
  "workerMembers":[{"name":"dc1-lead","role":"team_leader"},
                   {"name":"dc1-dev","role":"worker"},
                   {"name":"dc1-br","role":"worker"}]}'

# 或 agt CLI
agt create team --name t-dc1 --leader-name dc1-lead --workers dc1-dev,dc1-br --collaboration direct
```

生效面（建队后应看到）：
- MinIO `agents/<w>/AGENTS.md` = direct 目录版底版 + direct 协作块（含 "collaboration: direct"）
- `agents/<w>/skills/` = direct-work + 通用 skill（无 task-management/project-management/team-coordination）
- `agents/<w>/runtime/runtime.yaml` 的 `team.collaboration: direct`
- copaw 工具列表无 taskflow/projectflow、有 work_report
- bridge 沙箱 `opencode.json`/`cimicode.json` 的 `skills.paths` = `[skills-direct, skills-taskflow/communication, file-sharing, mcporter, organization]`；`taskflow` 不存在，`work-report` 存在

### 工作方式（agent 视角）

被 @ → 简短应答 →（长任务 work_report started）→ 干活 → 写 `shared/outputs/<worker>/...` → `filesync/agentteams-sync push` → work_report completed → Team Room @requester 回执附路径。

**Peer Mentions 规则**（direct worker/leader 模板均含）：peer @ 仅用于行动请求（交接/工作问题/评审请求）；**允许有实质进展的多轮交互**（澄清/补输入/评审修正/后续行动），但每轮必须带新信息或新行动；**连续两轮无新信息或新行动即停止**，带部分结果回 requester；peer 不响应/含糊/重复同问时不再 ping，升级给 requester；peer 交互不建任务/项目/任务目录。

### 状态上报对接（可选，本期不列验收项）

与任务事件同构：env `AGENTTEAMS_STATUS_WEBHOOK_URL`/`_TOKEN` 或 `teams/<t>/shared/config/webhook.json`（per-call 拉取）。事件值 `direct_work_started`/`direct_work_completed`，`work.deliverables[]` 为共享相对路径，接收方顺 `teams/<team>/shared/` 取结果。详见 spec §4A。

## 6. 坑与边界（105 实测发现并已处理）

1. **CRD pruning**：未 apply 含 collaboration 的 CRD 时，字段被 K8s 丢弃，direct 完全不生效。**部署必须先 apply CRD**。
2. **成员 worker 底版**：team reconcile 只换协作块曾导致成员仍 taskflow 底版——已修（`dce68a3f`，成员也做底版/skills 重投影）。
3. **两 reconciler 互刷**：WorkerReconciler（team-agnostic）每轮把 direct 资产覆盖回 taskflow——已修（`5490e93d`，worker reconcile 也解析 Team collaboration）。
4. **成员协作块被覆盖为 standalone**（回执锚 @manager）：WorkerReconciler 不传 team 上下文导致 standalone 协作块——已修（`770b6534`，TeamName/TeamLeaderName 传入 prepare）。
5. **communication skill 路由拉力**：copaw worker 首条回执曾锚 Team Leader——已修（`f9c1761f`，两线 communication 增 direct requester 段）。
6. **历史自模仿**：旧房残留修复前回执格式时模型会模仿旧格式（第二条起自行纠正）；全新房无此问题，生产新队无历史污染。
7. **standalone 协作机制不受影响**：无 runtime.yaml/team 段 → taskflow 工具集原样；golden-standalone 仅随通用短答案条款同步，协作块与渲染结构不变。
8. **模式切换残留**：skills 互斥 DeletePrefix、AGENTS/HEARTBEAT/SOUL 按模式重投影、沙箱 paths 重写 + wrapper mv 可逆，均已双向实测。
9. **direct 不进 dashboard 项目看板**：不写 `shared/projects/`；看板消费走 webhook/日志（上报为可选项）。
10. **bridge leader 不在生效链**：生成器 role 白名单仍仅 worker/standalone；direct leader 生效面=copaw team-leader-agent-direct。
11. **skill hub 分发**（内网）：direct 队技能集 = 通用 skills + direct-work（不含任务协议 skill）；hub 覆写 `/opt/agentteams/skills` 同路径的约束不变；`work-report` wrapper 指向 direct 目录脚本路径，hub 须携带 direct-work 的 scripts 全套（work_report.py + status_report.py + mc_sync.py）。
12. **共享写冲突**：`mc mirror --overwrite` 同名覆盖无合并——per-worker outputs 目录隔离是硬约定。
13. **镜像运维**：多节点环境 import 只落本节点（工作负载钉 extvdiadmin）；磁盘 >85% 触发 imageGC 回收未引用镜像（清出安全水位）；同 tag 重建镜像后需 `rollout restart` 才能让 pod 用新内容；反复 prune/import 可能出现"index 在 blob 缺"，需 `ctr images rm` + 重导。
14. **Ark Host 校验与 Higress 不兼容**：Higress agentteams 模式转发 Host=服务 DNS 名，火山 Ark 要求官方 Host（`Direct IP access is not allowed`）。105 通过 `ark-proxy`（Host 重写 + chunked 支持）中继解决；内网部署需同样前置或上游修复。

## 7. 验证场景与结果

| # | 场景 | 结果 |
|---|---|---|
| S1 | direct 队生效面 | ✅ 成员 AGENTS=direct 目录版；skills=direct-work+通用（无任务协议项）；runtime.yaml collaboration=direct；沙箱 paths=direct 目录+通用子集、taskflow 停用 |
| S2 | @copaw worker 派活 | ✅ 直接干活无 taskflow 调用；`teams/<t>/shared/outputs/<w>/` 落文件；Team Room @requester 回执附路径 |
| S3 | @bridge worker 派活 | ✅ 同 S2（沙箱 work-report + agentteams-sync 路径） |
| S4 | @copaw leader 派活 | ✅ leader 自己干活不委派；交付物/回执同上；不产生 projectflow |
| S5 | 纯 Q&A | ✅ 直接回答，无 work_report 调用 |
| S6 | 媒体归档 | ✅ 群内发文件仍由 copaw leader 归档 `shared/knowledge/matrix/` |
| S9 | 双成员并行 | ✅ 两 worker 同时被 @ 干活，各自 outputs 目录无互相覆盖 |
| S10 | taskflow 队回归 | ✅ 默认建队全链（create_project→delegate→ack→submit→TASK_COMPLETED→验收）与现状一致 |
| S11 | 模式切换 | ✅ taskflow↔direct 双向切换后 AGENTS/skills/沙箱/wrapper 均正确，无残留 |
| S12 | standalone 回归 | ✅ standalone worker 工具集/行为不变 |
| S7 | 凭据协议 | 未专项实测（模板锚点已改 requester，静默规则沿用 v5） |
| — | direct_work 事件（可选） | ✅ dc1 三成员事件到 sink（deliverables/requester/status/Bearer 正确），不列本期验收项 |

## 8. 交付物

- 代码：`origin/dev-v1.2.3`（`ec3962dc` 主实现 → `dce68a3f`/`5490e93d`/`770b6534` 修复 → `f9c1761f` communication → `30b0682c`/`55f84d35` 目录拆分 → `00620d1b`/`dbe52acb` Peer Mentions），含全部测试与文档
- 镜像：agentteams-controller、copaw-worker、worker-bridge、opencode-runtime / cimicode-runtime、worker-bridge-operator（tag `v1.2.3-dc*`）；cimicode-runtime 内网构建配方相同
- 资产：direct 模板与 direct-work skill（含内网 skill 分发包）
- 文档：本文件、spec §4A、105 验收报告（`AgentTeams-direct协作模式-105验收报告.md`）
