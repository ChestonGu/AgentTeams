# opencode 运行时替换与协作流转详解

> 面向开发的完整技术文档：替换原理、组件职责、端到端流转、数据与参数传递、协作协议、加固点。
> 分支 `dev-v1.2.2-opencode-ben-test`，测试环境 `opencode-team-test`（105 k3s）。
> 镜像基线：controller `v1.2.2.6-oct` / bridge `v0.1.9-oct` / opencode-runtime `v0.1.3-oct` / opencode-sandbox `v0.1.2-oct` / opencode-stack-operator `v0.1.6+` / copaw-worker `v1.2.2.2`（leader 专用）。

---

## 一、替换原理：worker 运行时怎么从 copaw 换成 opencode

### 1.1 核心思路

**只换 worker，不换协作体系**。团队协作的全部协议（Matrix 房间、taskflow 任务状态机、MinIO 文件协作、结构化 mention）保持 copaw 既有语义；唯一改变的是 worker 侧"大脑与工具载体"：

| | copaw worker（原） | opencode worker（新） |
|---|---|---|
| 大脑 | copaw runtime（agentscope） | opencode serve（REST headless） |
| 工具 | copaw 内置 tool call | opencode 工具 + bash 路由到 sandbox CLI |
| system prompt | copaw 拼 AGENTS.md+SOUL+PROFILE | **bridge 每 turn 生成 agent.md 注入** |
| leader | copaw | **仍是 copaw（不迁移）** |

### 1.2 关键机制：worker 伪装（masquerade）

controller 不感知"opencode"这个运行时（CRD enum 也没有它）。实现方式：

1. Worker CR 不写 `spec.runtime`（留空），controller 经 env `AGENTTEAMS_DEFAULT_WORKER_RUNTIME=opencode` 兜底解析（`ResolveRuntime`），REST 创建路径不回写 CR（f1eda21 修复）。
2. controller 发现 worker 解析为 opencode 时，**把 `AGENTTEAMS_OPENCODE_BRIDGE_IMAGE` 指向的 bridge 镜像当作 worker 镜像**创建 pod（`kubernetes.go OpenCodeBridgeImage`）——bridge pod 就是 worker pod，复用全部 worker 生命周期管理（CR finalizer、SA token、env 注入、矩阵账号供给）。
3. `opencode-stack-operator` watch 同一 Worker CR，为它创建 opencode 运行栈（见 §4.4）。

```
Worker CR (runtime 留空 → opencode)
   ├─ controller  → 创建 agentteams-worker-<w> pod（镜像=cimicode-bridge）
   ├─ operator    → 创建 opencode-<w> + opencode-<w>-sandbox deployment/svc/secret
   └─ Team CR     → 把 <w> 拉进团队，写 runtime.yaml，建/进 Matrix 房间
```

### 1.3 组件拓扑

```
                       Matrix (Synapse)
                    ┌───────┴────────┐
              leader(copaw)      bridge worker pod
              pl-lead 型          agentteams-worker-<w>（cimicode-bridge）
                    │                │ matrix sync(mention 过滤)
                    │  @mention      │
                    │                ▼
                    │            opencode adapter (HTTP)
                    │                │
                    │      ┌─────────┴──────────┐
                    │      ▼                    ▼
                    │  opencode-<w> svc     opencode-<w>-sandbox svc
                    │  (opencode serve)     (sandbox helper :4097)
                    │  LLM 直连 zhipu        bash/exec + AGENTS.md 落盘
                    │      │                    │
                    │      └────共享 hostPath /workspace────┘
                    ▼
              MinIO (teams/<team>/shared/tasks/... 协作文件)
```

---

## 二、端到端大流程

一次"委托→交付→验收"的完整流转（r5 实测时间轴）：

```
[1] 建 Worker/Team CR
    controller: 注册 Matrix 用户 → 首写 runtime.yaml → 注册完补写 matrixUserId
    controller: 【就绪屏障】runtime.yaml 未全量前 deferring，不建 bridge pod
    operator:   建 opencode/sandbox deployment + svc + FS secret + CR env 接线
    controller: runtime.yaml 全量后建 bridge pod（env 齐备）
    bridge:     bootstrap 拉 MinIO 配置 → 自算接线 → matrix sync 建立 → listening

[2] admin/manager 在团队房间 @leader 发项目委托（结构化 mention）
    leader(copaw): 立项（projectflow）→ 拆 DAG → delegate_task 派发
    └─ 系统自动在房间 @<worker> 发任务指派消息（结构化 mention）

[3] bridge worker 收到 mention → turn
    mention 过滤 → 每 turn 现拉 bootstrap → 生成 agent.md → 注入 opencode
    opencode 跑 LLM+工具（skill/taskflow/读写共享卷）→ 中间进度逐条回房间
    → taskflow ack → 实现 → taskflow submit（写 MinIO + 校验）

[4] worker TASK_COMPLETED（结构化 mention @leader）
    leader: taskflow check 拉结果 → 实际核对交付物/跑测试 → plan 节点 [~]→[x]
    → （DAG 依赖解锁下一波）→ 全 [x] → 项目 result.md → 完结报告 @manager

[5] 删除 Team/Worker CR → finalizer 退房/清栈（operator GC deployment/svc）
```

r5 实测：单任务队 8 分钟完结（b5）；并行队含一次 BLOCKED 自愈 24 分钟完结（a5）。

---

## 三、bridge 详解（cimicode-bridge v0.1.9）

### 3.1 模块结构

```
cimicode_bridge/
├── app.py            # BridgeApp：生命周期、turn 编排、自愈收敛、HTTP API
├── bootstrap.py      # S3Bootstrap：MinIO 配置拉取（openclaw.json/runtime.yaml）
├── prompt.py         # 生成器子进程包装（build_agent_md_via_generator）
├── matrix/gateway.py # MatrixGateway：sync 长轮询、发消息、typing
├── matrix_client.py  # MentionFilter：mention 判定与角色门禁
├── runtime/opencode_adapter.py  # opencode REST 适配器
└── session.py / store/          # 会话与 since 游标持久化
```

### 3.2 初始化收敛（三层防线，永久解决"首条消息搁浅"）

bridge pod 能回复需要三类信息，历史上它们由三个作者异步写入，曾长期存在"pod 创建早于配置就绪→永久卡死"：

| 信息 | 作者 | 竞态后果（历史） |
|---|---|---|
| `BRIDGE_RUNTIME_ADAPTER/_BASE_URL/_HELPER_URL`（pod env） | operator patch CR env → controller 注入 | adapter 缺省 cimicode → 无 gateway → 卡 bootstrap |
| `agents/<w>/runtime/runtime.yaml`（MinIO） | controller（先写缺 matrixUserId 的版本，注册完重写全量） | 拉到半成品 → agent.md 生成 fail-loud → 首条 mention 搁浅 |
| matrix token（pod env） | controller 建 pod 时 | 无 token → 无 gateway |

三层防线（v0.1.9 + controller v1.2.2.6）：

1. **controller 就绪屏障**：`member_reconcile` 在 `wb.Create` 前，对 managed runtime 调 `Deployer.RuntimeConfigReadyForBootstrap(ctx, runtimeName)`——读 `agents/<w>/runtime/runtime.yaml` 并 Unmarshal，`member.matrixUserId` 非空才放行；否则 `RequeueAfter: 5s`。**bridge pod 诞生时刻配置必已全量**。
2. **bridge 接线自给**：`_derive_opencode_urls()` 从 `AGENTTEAMS_WORKER_NAME` 自算 `http://opencode-<w>-svc:4096` 与 `http://opencode-<w>-sandbox-svc:4097`；adapter 类型从 runtime.yaml 的 `member.runtime` 判定（`managed_runtime_type()`）。operator 的 env 从"必需品"降为"覆盖项"（`_explicit_base_url/_helper_url` 标志保证显式值优先）。**operator env 晚到不再能卡死任何 bridge**。
3. **自愈轮询兜底**（`_recover_late_runtime_wiring`，15s 周期）：bootstrap 对象缺失时每轮重拉 S3，拉到即重建 gateway/adapter（含 runtime.yaml→adapter 判定）；同时轮询 `GET /api/v1/workers/{self}` 的 `runtimeEnv`（controller 如实返回 CR spec.env 的 BRIDGE_RUNTIME_*），env 变化即重建。防未来新增配置作者重演时序问题。

### 3.3 turn 数据流（每条 mention 一次 turn）

```
matrix event (m.room.message)
  → MentionFilter.evaluate(body, sender, content)         # §3.4
  → HistoryStore.build_context(f"{sender}: {body}")        # 历史上下文包裹
  → start_typing → turn start 日志
  → 【每 turn 现拉】s3_bootstrap.load(retries=1)：          # v0.1.9 起
        runtime.yaml（identity/team 事实 + inlineConfig soul/identity）
        失败降级用启动缓存
  → build_agent_md_via_generator()                          # 子进程调生成器
        输入：--runtime-config（临时文件） --soul-file --profile-file
        （soul/profile 来自 runtime.yaml desired.inlineConfig 的投影，
          legacy copaw 路径则读 agents/<w>/SOUL.md、PROFILE.md 文件）
        输出：agent.md 文本；exit≠0 → GenerateAgentMdError → 拒 turn
  → s3 publish agents/<w>/agent-md/latest.md（审计可见，带 sha256 日志）
  → runtime_client.chat(session_id, sandbox_id, turn_id, agent_md, ...)
  → opencode adapter（§3.5）
  → 事件流：TEXT_DELTA / progress_texts / TURN_COMPLETED
  → progress 逐条 send_text → 最终回复 send_text（自动补 m.mentions）
```

关键点：**agent.md 每个 turn 重新生成、输入每个 turn 现拉**——controller 对 runtime.yaml 的任何事后补写（matrixUserId、团队变化）即刻生效，不存在陈旧缓存。

### 3.4 Matrix 防护（mention 过滤与角色门禁）

`MentionFilter.evaluate` 的判定顺序与拒绝原因：

```
1. role = role_resolver.role_for(sender)
   self            → 拒（self_message）        # 自己发的消息绝不触发 turn
2. 非 mention 判定：mentions_self()
   收集三源：content.m.mentions.user_ids
            formatted_body 的 matrix.to/#/@user 链接
            body 正则 @name(:domain)?（短名也认）
   任一命中 self（或别名 leader/manager/team）才算被 mention
   未命中 → 拒（not_mentioned）但 allowed_roles 的消息进 HistoryStore
            作为后续 turn 的上下文（房间对话完整性）
3. role==unknown 且未开 allow_unknown → 拒（unknown_sender）
4. group 房间且 role 不在 {leader, admin, human} → 拒（sender_not_allowed）
   （worker 之间不能互相触发 turn——协作必须经 leader 中转）
5. 通过 → accepted
```

其它 matrix 层防护：

- **sync 游标持久化**（`matrix:since:<worker>`，state store）：bridge 重启后从上次位置续传，**未消费的 mention 重放**（重放即自愈：r4 被搁浅的任务在 pod 重建后自动续跑）；已消费的不重复触发。
- **token 自动刷新**（`_refresh_matrix_token` → controller `POST /api/v1/credentials/matrix-token`）。
- **on_authenticated 喂身份**：gateway 认证完成即把 whoami 结果写进 filter，首个事件就能正确判 mention（曾因身份竞态丢消息）。
- **回复自动补结构化 mention**（send_text 解析 body 中 @短名 → joined_members 索引 → m.mentions.user_ids）：worker 的 TASK_COMPLETED 能可靠唤醒 copaw leader（leader 侧群聊 requireMention 只认完整 MXID）。
- **房间失败可见**：turn 异常（runtime_error/turn_interrupted/超时）必发 `**turn failed**: <原因>` 到房间，绝不让房间静默死等。
- **turn 超时**：默认 3600s（`BRIDGE_RUNTIME_TURN_TIMEOUT` 可覆盖）。opencode 的 `POST /session/{id}/message` 阻塞到整个 turn 结束，超时只说明 bridge 放弃等待，opencode 侧任务仍在跑（错误文案明示 "ask again to re-attach"）。

### 3.5 opencode adapter（runtime/opencode_adapter.py）

```
chat(session_id, sandbox_id, turn_id, agent_md, ...):
  1. POST {helper_url}/agents-md   body=agent_md
     → sandbox helper 写共享卷 /workspace/AGENTS.md（opencode 原生读取规则文件）
  2. POST {base_url}/session       → 复用/新建 session（ses_...）
  3. POST {base_url}/session/{id}/message   （阻塞整个 turn）
  4. 轮询 GET /session/{id}/message 收尾：baseline 之后全部已完成 assistant
     文本 → 首条为最终回复，其余作为 progress_texts（房间中间进度）
  5. 组事件 [TEXT_DELTA(全文), TURN_COMPLETED(+progress)]
  异常映射：httpx.TimeoutException → RUNTIME_ERROR（可读文案）；其余 → 空错误
  也补全为 "opencode adapter failure: <type>"
```

session/sandbox 校验对 opencode 模式放宽（session 自管，无需预建 gateway session）。

---

## 四、controller 详解（v1.2.2 → v1.2.2.6 的 opencode 相关改动）

### 4.1 修改清单

| 版本 | 改动 | 位置 |
|---|---|---|
| v1.2.2 | REST 创建不回写 `spec.runtime`（opencode 靠 env 兜底，避开共享 CRD enum 校验） | resource_handler / reconciler |
| v1.2.2.4 | bridge 伪装建 pod（`AGENTTEAMS_OPENCODE_BRIDGE_IMAGE`）、copaw 工具护栏默认禁（aec5c7a）、createRoom 自邀 403 修复（000362b）、invite/join 幂等、MinIO attach 幂等 | backend / matrix |
| v1.2.2.5 | **pod-template env hybrid**：`ApplyPodTemplate` 的 env 从"controller 整体覆盖"改为 `mergeEnvVars`——ConfigMap `pod-template.yaml` 里 worker 容器的 env 先落，controller 同名覆盖。运维在 ConfigMap 一处配 `COPAW_TOOL_GUARD_ENABLED=false`，**所有** agent pod（含新建 leader）自动获得，不再需要逐 CR patch | agent_pod_template.go |
| v1.2.2.6 | **就绪屏障**（§3.2 第 1 层）+ `WorkerDeployer` 接口新增 `RuntimeConfigReadyForBootstrap` | member_reconcile.go / runtime_config.go |

### 4.2 runtime.yaml（MemberRuntimeConfig）——agent.md 的唯一事实源

controller 的 TeamReconciler 为每个 managed worker 写 `agents/<w>/runtime/runtime.yaml`：

```yaml
apiVersion: agentteams.io/v1beta1
kind: MemberRuntimeConfig
metadata: {generation, updatedAt}
team: {name, leader, members[](name,role,matrixUserId), admin}   # Coordination 块数据
member: {name, runtimeName, role, runtime, matrixUserId, personalRoomId}
desired:
  inlineConfig: {soul, identity}          # Worker CR 的 spec.soul/spec.identity 投影
  model: {...}
storage: {provider, bucket, endpoint, teamPrefix, sharedPrefix, ...}
credentials: {matrixTokenEnv, gatewayKeyEnv, ...}   # env 名约定，不是值
```

生成器（`opencode/bridge/generate_agent_md.py`，随 bridge 镜像分发）以它为 `--runtime-config` 输入渲染 agent.md：`{{COORDINATION}}`（团队协作块，文案与 copaw coordination.go 对齐）+ `{{ENVIRONMENT}}`（worker 身份/存储/Matrix 事实）+ Persona 段（soul/identity 逐字附加，带"不覆盖 Worker 角色"护栏导语）。fail-loud：缺身份/缺 team_leader/占位符数不对/残留 `{{` → exit 1 → bridge 拒 turn。

> agent.md 输入**只有** runtime.yaml + soul/identity（用户自定义 AGENTS/TOOL/IDENTITY merge 曾实现过又被回退，当前明确不支持）。

### 4.3 worker pod env 注入总表（bridge 视角）

| env | 注入者 | 用途 |
|---|---|---|
| `AGENTTEAMS_WORKER_NAME` | controller | 一切自算的锚点（S3 前缀、URL 派生） |
| `AGENTTEAMS_CONTROLLER_URL` / `AGENTTEAMS_AUTH_TOKEN(_FILE)` | controller | 自愈轮询 / token 刷新（SA projected token） |
| `AGENTTEAMS_WORKER_MATRIX_TOKEN` | controller | matrix 登录 |
| `AGENTTEAMS_FS_ENDPOINT/_BUCKET/_ACCESS/_SECRET_KEY` | controller | bootstrap/现拉/agent-md 发布 |
| `AGENTTEAMS_AI_GATEWAY_URL` | controller（chart helper） | copaw 用；opencode 不经网关 |
| `BRIDGE_RUNTIME_ADAPTER/_BASE_URL/_HELPER_URL` | operator→CR env | **现为覆盖项**，bridge 可自算 |
| `COPAW_TOOL_GUARD_ENABLED` 等 | pod-template ConfigMap（hybrid） | 只对 copaw 有意义，随模板注入所有 agent pod |
| `AGENTTEAMS_RUNTIME_TURN_TIMEOUT` | 部署层 | turn 超时覆盖 |

### 4.4 opencode-stack-operator（opencode/operator/opencode_stack_operator.py）

watch 本 ns 的 Worker CR（opencode 型），为每个 worker 供给独立运行栈并持续 reconcile（10s）：

- `opencode-<w>` Deployment+svc（镜像 `OPENCODE_IMAGE`，cwd=/workspace 共享 hostPath）
- `opencode-<w>-sandbox` Deployment+svc（镜像 `SANDBOX_IMAGE`；env：FS 四件套、`AGENTTEAMS_TEAM`、`AGENTTEAMS_MATRIX_USER_ID`；secret `opencode-sandbox-fs-<w>`）
- patch Worker CR `spec.env` 补 `BRIDGE_RUNTIME_*`（指向上述 svc）
- CR 删除时 GC deployment/svc/secret
- env `NODE_SELECTOR_HOSTNAME` 控制两个 deployment 钉节点

> 注意：operator 直接 `kubectl set image` 会被它在 10s 内改回——改镜像必须改 operator 的 env。

---

## 五、opencode runtime / sandbox 与 skill 机制

### 5.1 镜像内容

- **runtime（v0.1.3-oct）**：node:22-slim + `opencode-ai@1.18.x` + **ripgrep**（skill 工具依赖，缺它每次 skill 调用挂 133s 后报 "ripgrep execution failed"）+ 自定义 `opencode.json`（LLM 直连 zhipu Coding 端点，**不经 higress 网关**）+ 自定义 bash 工具（转发 sandbox）。
- **sandbox（v0.1.2-oct）**：python3/mc/jq + 全套 skills + taskflow/agentteams-sync CLI + sandbox_helper（:4097，`POST /exec` 执行命令、`POST /agents-md` 落盘 AGENTS.md）。
- **共享卷**：两 pod 挂同一 hostPath `/workspace`——文件协作的唯一可靠通道。

### 5.2 skill 生效链（曾两度失效，现为三层保障）

1. sandbox entrypoint 启动时把镜像内 skills 同步到 `/workspace/.opencode/skills/`（opencode 项目级原生发现位置）；
2. **runtime entrypoint 启动前等待该目录出现**（`SKILL_WAIT_SECONDS=180` 超时降级）——消灭"sandbox 晚于 runtime 发布→opencode 启动时 skill 工具为空"的竞态；
3. 镜像带 ripgrep——skill 工具自身依赖可用。

实测：自然流程（任务书不提 skill）worker 按模板 §4 协议自然调用 skill 37-57 次、零错误。

### 5.3 跨 pod 边界（重要约束）

bash 工具在 **sandbox pod** 执行（其 /tmp 是 sandbox 的），read/write/glob 等在 **runtime pod** 本地执行——两 pod 仅 `/workspace` 共享。任何"bash 写 /tmp 再 read 读"的路径都会挂死。AGENTS.md 模板**不写**这类环境细节（模板纯净性约定），环境问题一律部署层解决。

---

## 六、任务协作协议（taskflow / MinIO / 状态机）

### 6.1 存储布局

```
agentteams-storage/
├── agents/<w>/agent-md/latest.md        # bridge 每 turn 生成的 agent.md（审计）
├── agents/<w>/runtime/runtime.yaml      # controller 写（见 §4.2）
└── teams/<team>/shared/
    ├── plan.md                          # DAG 计划（[ ]/[~]/[x] 三态）
    ├── result.md                        # 项目结果（完结时）
    └── tasks/<task-id>/
        ├── spec.md                      # 任务书（leader 写）
        ├── meta.json                    # 状态机（assigned→in_progress→submitted）
        ├── result.md                    # worker 提交结果（SUCCESS/FAILED+deliverables）
        └── workspace/                   # 交付物
```

### 6.2 状态机与命令

`taskflow ack <id>`（拉任务目录+读 spec+置 in_progress+推送）/ `taskflow submit <id> --status ... --summary ... --deliverables ...`（写 result+推送+校验落盘）/ `taskflow check <id>`（leader 验收读回）。`--deliverables` 为单参数多值（重复传参会覆盖，只剩最后一个）。leader 侧 `projectflow` 管立项/DAG/完结。推送有排除护栏（spec.md/base/ 等协议文件不可被 worker 覆写）。

### 6.3 结构化 mention（协作可靠性的关键）

所有跨 agent 触发都必须是**结构化 mention**：`m.mentions.user_ids` + formatted_body 的 `matrix.to` 锚链接（纯文本短名 @ 对 copaw leader 不触发）。派单脚本、delegate_task、worker 汇报（bridge 自动补）均已遵循。

---

## 七、加固点总清单

| # | 加固 | 防什么 | 实现 |
|---|---|---|---|
| 1 | mention 三源判定 + 角色门禁 + self 拒绝 | 误触发/worker 互踢/自己触发自己 | MentionFilter（§3.4） |
| 2 | not_mentioned 进历史 | 房间上下文断裂 | HistoryStore |
| 3 | sync 游标持久化 + mention 重放 | bridge 重启丢消息 | state store since key |
| 4 | 回复自动补 m.mentions | worker 汇报唤不醒 leader | send_text 短名解析（d35d6da） |
| 5 | on_authenticated 喂身份 | 首事件 mention 判错 | gateway 回调 |
| 6 | turn 超时 3600s + 失败房间可见 + 可读超时文案 | 长 turn 静默死亡、房间死等 | f37e71b |
| 7 | bootstrap 三层收敛（屏障/自给/自愈轮询） | 初始化竞态永久卡死 | §3.2 |
| 8 | agent.md 每 turn 现拉 | 陈旧缓存 fail-loud 搁浅 | v0.1.9 |
| 9 | skill 三层保障（发布/等待/ripgrep） | skill 工具空或调用失败 | §5.2 |
| 10 | pod-template env hybrid | 新建 leader 撞 copaw 工具护栏 | v1.2.2.5 |
| 11 | rc_invites/rc_joins 放宽 | 批量退房 429 卡 finalizer 数小时 | synapse configmap |
| 12 | 资源三层限额（组件 limits/LimitRange/Quota） | runaway 容器拖死节点（103 两次 OOM 教训） | oct-resource-guards.yaml |
| 13 | 全组件 nodeSelector 钉 105 | 共享节点故障/镜像分发 | values |
| 14 | `*.sh eol=lf`（.gitattributes） | CRLF shebang 毁镜像入口 | 构建链 |
| 15 | bridge 日志带时间戳 | 排障盲查 | logging format |
| 16 | turn 失败/生成失败 fail-loud 拒 turn | 残缺 system prompt 发给 agent | 生成器 exit≠0 契约 |

---

## 八、测试结论（r1-r5）

- **r1**：流水线/并行/审查返工三团队——全链协作、状态机全路径（含打回返工复审）✅
- **r2**：三轮迭代/全栈/6 并发压力 ✅（暴露 skill 竞态、bootstrap 竞态、护栏、限流等 8 项问题，全部修复）
- **r3/r4**：修复回归——skill 自然调用 31-57 次零错误、护栏 ConfigMap 自动注入、ripgrep 修复 ✅
- **r5**：**零干预回归**——就绪屏障实录 deferring→放行、3 bridge 一次成型 listening、两队自然完结（含一次 BLOCKED→修复→继续的自愈协作）✅
- 证据：105 `~/oct-evidence/{r1..r5}/`（per-worker 统一时间线、agent.md+sha256、MinIO 树、pod 快照、manifest）

## 九、遗留与约定

- r5 的 a5-lead 曾手写任务文件（未走 delegate）导致 meta.json 字段名错——worker BLOCKED 上报→lead 修复闭环正常，但 leader 侧"应使用 projectflow delegate"的引导可再加强。
- opencode worker 的 LLM 直连 zhipu（不经网关）——网关日志看不到 worker LLM 活动属正常。
- 已知边界：跨 pod /tmp 不共享（§5.3）；`--deliverables` 单参数多值；operator 镜像改动必须走 operator env。
