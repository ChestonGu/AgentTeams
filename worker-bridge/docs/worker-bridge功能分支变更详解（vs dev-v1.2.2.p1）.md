# worker-bridge 功能分支变更详解（feature/worker-bridge-merge vs dev-v1.2.2.p1）

> 面向对象：需要在功能分支上继续开发、部署联调或代码评审的工程师。
> 基线：`origin/dev-v1.2.2.p1`（merge-base `5d949340`）。
> 文档基准：feature/worker-bridge-merge @ `da22acf4`（2026-09-14，已推 origin）。
> **统一形态注记（2026-09-17 后补，`dev-v1.2.3-cimi`）**：本文基准是统一形态
> 改造**前**的快照。其后六提交（`9929bfa1`…`f87d4ef4`，2026-09-16）已将文中
> 以下设计全部退役——Worker env 三键→两键（`BRIDGE_RUNTIME_HELPER_URL` 移除
> 并显式清除残留）、svc 双端口（:4096/:4097）→单端口 :4096、helper 链路
> （sandbox_helper / `/agents-md` 推送）→ agent.md 改走消息体 `system` 字段、
> emptyDir→容器可写层、`ZHIPU_API_KEY` build-arg 烘焙→镜像零凭据（operator 经
> `OPENCODE_CONFIG_CONTENT` secretKeyRef 注入）、新增 `opencode-runtime/`
> （cimicode 形态的外网模拟件，`cimicode-runtime/` 为本体）。文中这些段落按
> 历史设计阅读，现状权威：[README.md](../README.md)、
> [worker-bridge-pod模式与operator部署详解.md](worker-bridge-pod模式与operator部署详解.md)、
> [../contract/adapter-contract.md](../contract/adapter-contract.md) v1.1。

---

## 一、总览

### 1.1 一句话定位

本分支把此前**三个来源**的 worker 运行时（① opencode 双 pod 链路、② stateless
SSE gateway 链路、③ copaw 链路）统一收敛为 **`worker-bridge` 运行时**：一个由
controller 供给的 bridge 进程代理 Matrix 消息，按 `adapterMode` 双形态分派到
**外部 cimicode 平台（stateless）**或**集群内单 pod 合并镜像（cimicode-pod，
内部 cimicode = 换皮 opencode）**，并以 CLI + 技能模板 + 契约文档构成完整的
任务协作工具链。

### 1.2 规模与目录分布

| 指标 | 数值 |
|---|---|
| 提交数（无 merge） | 19 |
| 变更文件 | 154 |
| 代码量 | +22352 / −140 |

| 目录 | 文件数 | 内容 |
|---|---|---|
| `worker-bridge/`（新增顶层目录） | 99 | bridge 进程、合并镜像、operator、CLI、模板、契约、文档 |
| `agentteams-controller/` | 39 | worker-bridge 运行时接入 + 上游修复搬运 + 重试链修复 |
| `helm/agentteams/` | 10 | 镜像供数链 + CRD 同步 |
| 根（Makefile/.gitattributes/.gitignore/docs） | 4 | 构建目标、CRLF 防护、忽略项 |

### 1.3 提交时间线（新 → 旧）

| 提交 | 主题 | 一句话 |
|---|---|---|
| `da22acf4` | fix(controller) | Failed Team/Human 退避 guard 改为重排剩余窗口，修复重试链死亡 |
| `f949adae` | fix(bridge) | cimicode-pod progress 按位置截断 baseline，不回放 session 历史 |
| `781e149d` | fix(operator) | deployment_drift 的 volume 比较改用 is not None（幻影漂移） |
| `63578ceb` | fix(cimicode-runtime) | mc 改 vendored 二进制直供，去掉构建期下载 |
| `193b4166` | fix(make) | build-worker-bridge-operator 构建上下文改 operator 目录 |
| `75c61611` | feat(operator) | cimicode-pod 供给真实运行时（双端口/凭据 Secret/工作 env） |
| `daf4b28f` | feat(cimicode-runtime) | cimicode pod 合并镜像（opencode+协作工具+skills） |
| `43d115c3` | feat(bridge) | cimicode-pod adapter 重写为 opencode REST 轮询 |
| `998cc8b7` | refactor(repo) | bridge-runtime 并入 bridge——单目录承载进程+工具 |
| `73abde92` | Revert | minio service.type 改动的 revert（净效果：功能分支无此改动，见 §5.7） |
| `d596b075` | fix(helm) | minio service 尊重 values 的 service.type（后被 revert） |
| `e997987e` | feat(naming) | bridge/cimicode pod 角色后缀式命名 |
| `b67316bd` | refactor(repo) | cimicode-bridge 并入 worker-bridge/bridge-runtime |
| `ba367f11` | fix(controller) | cluster init registerAdmin 补重试 |
| `bfd04647` | feat(helm) | worker-bridge 镜像供数链（空默认 fail-fast）+ CRD/上游同步 |
| `5ea2e100` | feat(controller) | worker-bridge 运行时统一接入 + 上游修复搬运（37 文件，最大提交） |
| `f505bb8f` | feat(operator) | worker-bridge-operator 按 adapterMode 分派供给 |
| `cc531565` | feat(runtime) | worker-bridge 运行时资产目录落盘（CLI/模板/契约/文档/sandbox） |
| `a8fd9dca` | feat(bridge) | cimicode-bridge 落盘并统一为 worker-bridge 双 adapter |

### 1.4 顶层架构

```
                        Matrix (Synapse)
                             │  m.room.message（群/DM）
                             ▼
        ┌─────────────────────────────────────────┐
        │  bridge pod（agentteams-worker-<w>-bridge）│  controller 按 runtime=worker-bridge 供给
        │  cimicode_bridge/app.py                  │
        │   ├ matrix/filter.py    mention 过滤      │
        │   ├ prompt.py           agent.md 生成/推送 │
        │   └ runtime/registry.py adapter 裁决      │
        │        ├ cimicode_stateless_adapter ──────┼──► 外部 cimicode 平台（SSE gateway）
        │        └ cimicode_pod_adapter ────────────┼──► 集群内 cimicode pod
        └─────────────────────────────────────────┘          │
                                                             ▼
                                        <w>-cimicode（Deployment，合并镜像）
                                        opencode serve :4096 + sandbox helper :4097
                                        skills / taskflow / agentteams-sync
                                                    │
                                                    ▼
                                        MinIO teams/<t>/shared/（任务协作文件流）
```

供给链：`helm（镜像供数）→ controller（Worker CR → bridge pod + env 三键）→
worker-bridge-operator（adapterMode=cimicode-pod 时供给 cimicode Deployment/svc/
Secret + 回写 env 三键）→ bridge 自愈轮询接上`。

---

## 二、背景与关键设计决策

### 2.1 三来源收敛

| 来源 | 原形态 | 去向 |
|---|---|---|
| opencode 双 pod 链路（dev-v1.2.2-opencode-ben-test 演进） | worker pod + sandbox pod 两容器栈，opencode_stack_operator.py 供给 | CLI/模板/技能/镜像构建经验整体迁入 worker-bridge/；双 pod 栈退役，收敛为**单 pod 合并镜像** |
| stateless gateway 链路（feature/stateless_cimicode_docking，f990f857） | bridge 直调外部 cimicode 平台，SSE 事件流 | 完整保留为 `cimicode-stateless` adapter，SSE 基座不动 |
| copaw 链路 | 自带 Matrix channel 的常驻 agent | 不在本分支改造范围；作为 team leader 与 worker-bridge worker 混编协作（105 已七轮实测） |

### 2.2 关键决策与理由

1. **内部 cimicode = 换皮 opencode**：pod 形态脱离 SSE gateway 基座，独立实现
   opencode headless REST 契约（按 opencode **1.18.27** 标定）。
2. **轮询而非 SSE**：opencode 的 SSE 事件流在 headless 侧不可靠（丢事件/乱序），
   改为"阻塞 POST message + 轮询 session info 的 time.completed/error"。
3. **agent.md 每 turn 推送**（而非请求字段/启动时读一次）：pod 重建后会话与
   上下文全丢是**预期场景**，每 turn 经 helper `POST /agents-md` 原文重推 +
   session 404 自愈重建，保证组织视图（AGENTS.md coordination 段）始终最新。
   这也使 worker-bridge worker 天然规避了 copaw "长驻进程不热加载 prompt 文件"
   的问题。
4. **单 pod 合并镜像**：对话与执行同容器（node:22-slim + opencode + ripgrep +
   python3 helper + mc + 技能），无独立 sandbox pod；bash 执行经 custom tool
   `tools/bash.ts` 转发到容器内 helper `:4097 /exec`。
5. **绑定通道单一化**：Worker CR 与 bridge 之间**只**通过 runtime.yaml 顶层
   `bridge` 段 + Worker env 三键（ADAPTER/BASE_URL/HELPER_URL）绑定；绑定变更
   走 bridge 15s 自愈轮询热接，**不触发 pod 重建**（hashAppliedWorkerSpec
   排除 5 个绑定字段）。
6. **fail-fast 原则贯穿**：镜像双空 → Create 报错（防静默回落 openclaw 镜像）；
   helper_url 缺失 → RUNTIME_ERROR（绝不拿旧指令静默跑）；WATCH_NAMESPACE
   缺失 → operator 退出。

---

## 三、运行原理详解

### 3.1 一条消息的完整生命周期（cimicode-pod 形态）

```
Manager 在 team room 发 "@<worker> ……"
  → Synapse 事件 → bridge matrix/gateway.py 长轮询 sync 收到
  → matrix/filter.py：群内消息必须带 m.mentions.user_ids 命中本 worker
    （无 mention → reason=not_mentioned 丢弃；DM 同样要求 mention）
  → prompt.py：generate_agent_md.py 渲染 agent.md
    （源模板 AGENTS.md + runtime.yaml 结构化 + Persona 接缝合并），
    写 MinIO agents/<w>/agent-md/latest.md 留档
  → cimicode_pod_adapter.chat()：
     1) helper POST /agents-md 推送 agent.md 原文
     2) GET /session/{id} → 404 则自愈重建（pod 重建后会话目录丢失为预期）
     3) 记 baseline（最后一条 assistant 消息 id）
     4) POST /session/{id}/message —— 服务端阻塞至本 turn 完成
     5) 轮询 session info：completed/error/deadline 三态；
        baseline 之后新增的 assistant 消息 → progress_texts
  → 事件回放：[TEXT_DELTA(全文), TURN_COMPLETED(progress_texts)] —— 与
    stateless 形态同构，app.py 聚合逻辑两形态共用
  → progress_texts 逐条发房间（worker 中途叙述），final reply 最后发出
```

### 3.2 端口与环境变量契约

| 项 | 值 | 说明 |
|---|---|---|
| opencode serve | `:4096` | `opencode serve --port 4096 --hostname 0.0.0.0` |
| sandbox helper | `:4097` | `/healthz`、`/agents-md`（原子写）、`/exec` |
| svc 双端口 | runtime:4096 / helper:4097 | `<w>-cimicode-svc`，operator 供给 |
| Worker env 三键 | `BRIDGE_RUNTIME_ADAPTER` / `BRIDGE_RUNTIME_BASE_URL` / `BRIDGE_RUNTIME_HELPER_URL` | operator/controller 回写 Worker CR env；bridge 自愈轮询热接 |
| bridge env 覆盖 | `BRIDGE_RUNTIME_*` 同名 env | `_apply_env_overrides()`，优先级高于 runtime.yaml |
| cimicode pod 工作 env | WORKER_NAME / FS_ROOT(/workspace) / FS_ENDPOINT / FS_BUCKET / TEAM / MATRIX_USER_ID / SANDBOX_EXEC_URL / OPENCODE_PORT | **绝不设 AGENTTEAMS_RUNTIME**——mc 同步只认 k8s/aliyun，本 pod 走 local 静态三元组 |
| MinIO 凭据 | Secret `<w>-cimicode-fs`（secretKeyRef） | operator 从 bridge pod env 明文双候选 pod 名读取复制 |

### 3.3 adapter 裁决顺序（§3.3，两处提交共同定义）

```
显式 BRIDGE_RUNTIME_ADAPTER env
  > runtime.yaml bridge.adapterMode（空规范化为 cimicode-pod）
  > 未定态：不建 client，15s 自愈轮询等待接线（controller/operator 回写 env 后热接）
```

### 3.4 任务协作文件流（MinIO）

```
teams/<t>/shared/
├── tasks/<project>-<seq>/        # taskflow 协议：meta.json 状态机
│   ├── (assigned → in_progress → submitted)   acknowledged_at/submitted_at
│   └── 交付物（代码/测试/README/报告）
├── projects/<project-id>/        # meta.json / plan.md（DAG）/ result.md
└── agents/<w>/agent-md/latest.md # bridge 每 turn 生成的 agent.md 留档
```

leader（copaw）用 projectflow 建项目/DAG/派单（delegate_task），worker
（worker-bridge）用 taskflow check/ack/submit 推进，验证（复跑测试、只读哈希
基线冻结）与验收均基于共享对象。105 七轮实测（含并行派发、故障注入恢复）
见 §6.2。

---

## 四、分模块详解

### 4.1 bridge 进程（`worker-bridge/bridge/`）

**目录**：`src/cimicode_bridge/`（app.py 602 行主链路；matrix/ filter+gateway；
runtime/ 双 adapter + registry + turn；store/ file/memory/redis；api/ 探针路由；
bootstrap.py 解析 runtime.yaml）+ `generate_agent_md.py` + `agentteams_log.py`
（部署到 /opt/agenttools/）+ Dockerfile（上下文=仓库根）+ tests/（生成器
golden）+ tests/unit/（进程 pytest，115 用例）。

**演进三步**：
1. `a8fd9dca` 落盘：以 feature/stateless_cimicode_docking（f990f857）为基线，
   CimicodeAdapter 子类化为 **Stateless**（直调外部平台）与 **Pod** 两个
   adapter，公共 SSE/协议逻辑留基类；硬编码地址/口令全部收敛为 env/config。
2. `43d115c3` 重写 pod adapter：opencode REST 轮询契约、agent.md 走 helper、
   session 自愈、progress_texts；`config.RuntimeConfig` 加 `helper_url`；
   `BRIDGE_RUNTIME_HELPER_URL` 进 env 覆盖与**晚到接线自愈**（docstring 声明
   过但未实现的 _HELPER_URL 补齐）。新增 contract/adapter-contract.md。
3. `f949adae` 修 progress 历史回放（见 §5.1）。

**关键行为（105 实测确认）**：
- 无 mention 消息（含 leader 群内广播）一律 `not_mentioned` 忽略；
- bridge 冷启动 initial sync 会**补消费挂机期间错过的 mention 消息**
  （派发零丢失）；
- turn 失败 fail-loud：日志 `Gateway turn failed` **且向房间发
  `**turn failed**: <原因>`**，leader 可感知；
- 恢复后 leader 广播被 worker 以 `NO_REPLY` 正确静默（不抢话）。

### 4.2 cimicode-runtime 合并镜像（`worker-bridge/cimicode-runtime/`）

`daf4b28f` 新建 + `63578ceb` mc vendored。Dockerfile（node:22-slim）：
- apt：ca-certificates tini **ripgrep**（skill 工具必需，缺则每次挂 130s）
  python3 curl jq git procps；mc 为 **vendored 二进制直供**
  （`bin/mc` RELEASE.2025-08-13，dl.min.io latest 路径已 410 Gone 且内网无外网）
- `ARG OPENCODE_VERSION` 钉 1.18.27；`npm i -g opencode-ai`
- `ARG ZHIPU_API_KEY` **必填**，sed 烘焙进 opencode.json（仓库内是
  `__ZHIPU_API_KEY__` 占位，key 只出现在构建命令行，不进 git）
- COPY template/worker-bridge-agent/skills → /opt/agentteams/skills；
  taskflow/agentteams-sync PATH 包装器
- entrypoint.sh：发布 skills 到 `$WORKDIR/.opencode/skills/` → 种子全局配置
  （provider + bash.ts）→ 后台起 helper → `exec opencode serve`
  （180s skill-wait 轮询随单 pod 形态删除）

### 4.3 worker-bridge-operator（`worker-bridge/operator/`）

`f505bb8f` 初版（按 adapterMode 分派：stateless 零供给 / pod 供给）→
`75c61611` 真实运行时供给（637 行）→ `781e149d` drift 修复。

**reconcile 主流程**（runtime=worker-bridge 的 Worker CR）：
```
stateless → return（bridge 直调外部平台）
cimicode-pod（含空值规范化）：
  CIMICODE_IMAGE 缺失 → error log 跳过
  → team_for()（Team CR workerMembers 反查）
  → bridge_pod_env()（双候选 pod 名 agentteams-worker-<w>[-bridge] 读明文
    MinIO 凭据；bridge 未起 → 本轮推迟供给，下轮再试）
  → ensure_secret <w>-cimicode-fs → ensure_service（双端口）
  → ensure_deployment（工作 env + secretKeyRef + emptyDir /workspace +
    readiness GET /session + PROVISION_NODE_SELECTOR 通用钉节点旋钮）
  → ensure_worker_env（三键 patch 回写 Worker CR env）
GC：managed-by=worker-bridge-operator label；owner CR 消失/切 stateless/
  adapterMode 变更 → 回收 Deployment/svc/Secret
```

**deployment_drift**：对比 env（明文 map + secretKeyRef 存在性指纹）、
volumes（按卷类型**存在性**比较——见 §5.3）、node_selector。

### 4.4 controller（`agentteams-controller/`）

**`5ea2e100`（37 文件）——worker-bridge 运行时统一接入**：
- backend：`RuntimeWorkerBridge` 常量 + ValidRuntime/IsManagedRuntime 收编；
  镜像三段式 `spec.image > AGENTTEAMS_WORKER_BRIDGE_IMAGE(env) > 双空 Create
  报错`（fail-fast 防静默落 openclaw 镜像）
- api/v1beta1 + CRD（config/crd 与 helm/crds 双处同步）：runtime enum 加
  `worker-bridge`；WorkerSpec 新增 5 绑定字段（adapterMode/cimicodeGatewayUrl/
  sessionId/sandboxId/templateId，adapterMode enum cimicode-stateless|cimicode-pod）
- runtime_config.go：runtime=worker-bridge 且任一绑定字段非空 → runtime.yaml
  投影顶层 `bridge` 段（camelCase；adapterMode 空规范化 cimicode-pod；无绑定
  不投影，保持未定态等 env）
- worker_controller：`zeroWorkerBridgeBindingFields` 把 5 绑定字段从
  hashAppliedWorkerSpec 两变体排除——**绑定变更不触发 pod 重建**
- server API：create/update/response 三结构流转 5 字段（update 非空覆盖、
  空不动；runtime 原样落 CR 不在 create 期解析默认值）
- agt CLI：apply 从 zip manifest 双位置取 adapterMode
- **上游修复搬运**（官方路径不受 worker-bridge 影响）：matrix/synapse 账号
  重激活、oss sdk_admin、provisioner、agent-pod-template、element-web/
  postgres/synapse/minio 模板同步等 dev 分支演进

**`ba367f11`**：cluster init registerAdmin 补 3s/5min retry 包络（对齐
waitForOSS/waitForMatrix 兄弟步骤；ProvisionUser 幂等）。

**`da22acf4`（重试链死亡修复，重点）**：见 §5.2。

**`e997987e`（命名）**：bridge pod 追加 `-bridge` 后缀
（`agentteams-worker-<w>-bridge`）；Status/Delete/Start 经 workerPodNames 双名
级联（plain → -bridge）；Create 冲突检查覆盖两候选名，防换 runtime 后旧名
pod 滞留双活。cimicode pod 命名 `<w>-cimicode` / svc `<w>-cimicode-svc`。

### 4.5 helm（`helm/agentteams/`，`bfd04647`）

- values.yaml：`worker.defaultImage.workerBridge.{repository,tag}` **默认双空**
  （部署侧必须显式填；双空时 controller 对 worker-bridge Create fail-fast）
- `_helpers.tpl`：`agentteams.worker.workerBridgeImage`（repository 空渲染空串）
- controller/deployment.yaml：env `AGENTTEAMS_WORKER_BRIDGE_IMAGE`
- crds/：workers/managers CRD 与 config/crd 同步（runtime enum + 5 绑定字段）
- element-web/matrix/postgres/storage 模板：上游修复搬运同步

### 4.6 运行时资产（`cc531565` 落盘）

- `cli/`：taskflow（worker 侧 check/ack/submit）+ agentteams-sync（mc 三模式
  alias 同步）+ projectflow（leader 侧预置）+ 统一 JSONL 日志
- `template/`：worker 与 leader 两套模板（AGENTS.md 源模板占位符化，仅
  `{{COORDINATION}}`/`{{ENVIRONMENT}}` 两占位符；skills+scripts 部署副本）
- `contract/`：interface-contract.md **v2.4** + controller-handover.md **v2.4**
  + adapter-contract.md（两形态传输契约/端口/接线三键/已知限制）
- `docs/`：两份设计文档（运行时迁移方案 / 运行时替换与协作流转详解）
- `cimicode-sandbox/`：pod 模式运行时镜像构建上下文（历史形态，合并镜像
  落成后由 cimicode-runtime/ 承担）
- `.gitattributes` 钉 `*.sh eol=lf`（Windows 开发机 CRLF 会破坏容器内
  entrypoint/sed）

### 4.7 目录整理（`b67316bd` + `998cc8b7`）

cimicode-bridge → worker-bridge/bridge-runtime → 并入 worker-bridge/bridge：
git mv 保历史；镜像名 `agentteams/cimicode-bridge`、values/_helpers/CRD 描述
不变。最终 `bridge/` = FastAPI 进程 src/ + 生成工具 + Dockerfile + 测试。

---

## 五、Bug 修复详解（现象 → 根因 → 修法 → 验证）

### 5.1 progress 历史回放（`f949adae`，bridge）

- **现象**：105 实测 worker 每 turn 回复携带全部历史叙述，bridge log
  `progress=0,1,2,3...` 单调增长；单 turn 出现 12 条重复叙述。
- **根因**：`_completed_assistant_texts` 只排除 baseline 那一条 **id**——
  baseline **之前**的历史 assistant 回复也是已完成消息，被全量收进本 turn
  的 progress_texts，每 turn 回放整个 session 历史。
- **修法**：新增 `_slice_after_baseline` **按列表位置截断**（不是按 id 匹配）；
  `_poll_reply` 轮询窗口同接入——baseline 非空却缺失时宁可继续等 deadline，
  不扫全表（否则会把上一 turn 的回复当成本 turn 完成信号）。
- **验证**：回归测试 `test_progress_does_not_replay_history`（种子历史回复 +
  新 turn，断言 progress 只含本 turn 新增；假服务注入条件改 per-turn 幂等
  标志）。镜像 v0.3.4-wb 实测 progress 恒为本 turn 真实计数（第五~七轮
  15+ worker-turn 复验）。

### 5.2 Failed Team 重试链死亡（`da22acf4`，controller，重点）

- **现象**：建 team 撞凭据竞态（team reconciler 先于 worker 凭据 Secret 跑
  存储刷新 → "credentials not found"）后，Team 卡 Failed **3 分 20 秒**无任何
  重试 pass，需人工 `kubectl annotate team <t> agentteams.io/retry=...` 唤醒。
- **根因（日志级还原）**：两次快速 failTeam（第二次常读 informer 旧对象直接
  绕过退避 guard）共享一个等待队列条目（workqueue 按对象 key 去重，第二次
  AddAfter 是 no-op）；唯一唤醒按**第一次**失败时间 +30s 触发，而 guard 拿
  **第二次**失败的 PhaseTransitionTime 比较（差 3.4s < 30s）→ 裸 `return`
  丢弃且**不排新唤醒** → 重试链永久死亡。
- **修法**：team_controller.go 与 human_controller.go 的退避 guard 丢弃分支
  改为返回 `reconcile.Result{RequeueAfter: 剩余退避窗口}`——唤醒链不再依赖
  可能已被消费的定时器。等待队列去重保证同一时刻仅一个待触发唤醒，不会
  放大重试频率；重试边界不变（30s 起步指数退避 30s/1m/2m/4m/8m cap 10min、
  最多 5 次 maxRetriesReached 后停等人工 annotate）。
- **并发安全**：workqueue per-key 互斥（同 Team 不会并发 reconcile，
  MaxConcurrentReconciles 只并行不同 Team）；修复代码零共享可变状态；
  重排风暴有界（item 已在等待队列时 AddAfter 是 no-op）；Status().Patch 有
  resourceVersion 乐观锁。
- **验证**：4 个回归测试 + 105 实测——t-fix1 撞竞态 **39 秒自愈**（修复前
  同场景 3m20s 卡死）；第七轮 3 team 一次性 apply 全撞竞态（t-p3 双重失败
  死亡时序），38s/59s/46s 全部自愈，**零人工干预**。
- **人工兜底命令**（maxRetriesReached 后重置重试预算）：
  ```bash
  kubectl -n <ns> annotate team <name> agentteams.io/retry=$(date +%s) --overwrite
  ```

### 5.3 operator 幻影漂移（`781e149d`）

- **现象**：每 pass 都报告 volume 漂移并反复空转 replace。
- **根因**：desired 侧 `empty_dir={}` 是 **falsy**，API 回读侧物化为
  `V1EmptyDirVolumeSource()` 实例是 **truthy**——`bool()` 比较永不相等。
- **修法**：改判**卷类型存在性**（is not None），附 API 物化回归测试。

### 5.4 mc 构建期下载下线（`63578ceb`）

dl.min.io 的 latest 式下载路径 410 Gone，真实内网构建无外网。从已验证镜像
提取 mc（RELEASE.2025-08-13T08-35-41Z，linux-amd64）vendor 进
`cimicode-runtime/bin/`，Dockerfile COPY 直装。

### 5.5 operator 镜像构建上下文（`193b4166`）

operator Dockerfile 的 `COPY worker_bridge_operator.py` 相对**上下文**解析；
仓库根上下文下该路径不存在。operator 镜像自包含单文件，上下文必须用
`worker-bridge/operator/` 目录（Makefile 已改）。注意与
build-cimicode-bridge / build-cimicode-runtime（仓库根上下文，COPY 带
`worker-bridge/` 前缀）**相反**。

### 5.6 cluster init registerAdmin 竞态（`ba367f11`）

新装集群：synapse 接受了建号 PUT 但 login 路径仍在预热，registerAdmin 单发
失败被记 non-fatal，admin 账号要等手动重启 controller。兄弟步骤
（waitForOSS/waitForMatrix）都带 retry，唯独它裸奔——套同一 3s/5min retry
包络（ProvisionUser 幂等，register-or-login）。

### 5.7 minio service.type：加入后 revert（`d596b075` + `73abde92`）

让 values 的 `storage.minio.service.type` 真正生效（默认 ClusterIP 时保留
headless `clusterIP: None`，NodePort/LB 时可钉 apiNodePort/consoleNodePort）。
在功能分支上先加入后被 revert（无原因记录），**净效果：功能分支无此改动**。
实现保留在测试分支 `test/worker-bridge-105`（5baabe4c）供 105 环境 NodePort
暴露 minio console 使用。如需回迁：cherry-pick 5baabe4c（默认行为逐字节
不变）。

---

## 六、测试与验证

### 6.1 单元/集成测试（feature 分支 @ da22acf4）

| 套件 | 结果 |
|---|---|
| agentteams-controller `go build ./...` | 通过 |
| agentteams-controller `go test ./...` | 全绿（oss 包 2 个预存在 Windows 环境失败除外：temp 生成的 mc 脚本 Windows 不可执行，与改动无关，git stash 验证过） |
| bridge pytest（tests/ + tests/unit/） | **115 passed** |
| operator pytest | **19 passed** |

### 6.2 105 真实环境七轮实测（cimicode-test ns，真实 LLM）

| 轮 | 主题 | 结论 |
|---|---|---|
| 1 | API 建 worker / 五轮上下文 / 边界输入 / 并发 / 附件 | ✓（附件 DM 是已知 gap） |
| 2 | csv-utils 全流程 + 容错链（坏 meta.json → fail-loud → leader 绕行恢复） | ✓ |
| 3 | dashboard 全流程（认证 wiring 修复 + 建栈 + 任务看板） | ✓ |
| 4 | 扩编/并行/追问/返工 | 扩编组织视图不刷新=copaw 侧已知限制；返工教科书级闭环 |
| 5 | 新协议（护栏禁用/独立 team/介绍核验/纯项目任务书）×2 项目 | ✓ 全绿 |
| 6 | 重试链修复验证（39s 自愈）+ team room 文件中继 | ✓ |
| 7 | 多维并发（3 team/11 worker 并发供给自愈/真并行派发/中途杀 pod 恢复/挂机补消费） | ✓ 八维度全过 |

### 6.3 镜像版本（105 现势）

cimicode-runtime `v0.1.0-wb105` / bridge（=agentteams/cimicode-bridge）
`v0.3.4-wb` / worker-bridge-operator `v0.2.0-wb105` /
agentteams-controller `v1.2.2.12-wb`（含 da22acf4）。

---

## 七、部署指南

### 7.1 镜像构建（Makefile targets）

```bash
# controller（上下文=agentteams-controller/，Makefile 会先 cp manager/agent → agent/）
make build-agentteams-controller

# bridge（上下文=仓库根）
make build-cimicode-bridge

# 合并镜像（上下文=仓库根；ZHIPU_API_KEY 只出现在命令行，不进 git）
make build-cimicode-runtime ZHIPU_API_KEY=<key>

# operator（上下文=worker-bridge/operator/，见 §5.5）
make build-worker-bridge-operator
```

内网环境需 `--build-arg NPM_REGISTRY=<mirror>`（cimicode-runtime）；
 higress chart 依赖 tgz 需拷进 charts/。go module 走 goproxy.cn。

### 7.2 helm 配置

```yaml
worker:
  defaultImage:
    workerBridge:
      repository: agentteams/cimicode-bridge   # 双空默认，必须显式填
      tag: v0.3.4-wb
```

controller deployment 自动获得 `AGENTTEAMS_WORKER_BRIDGE_IMAGE`。

### 7.3 Worker / Team CR 示例

```yaml
apiVersion: agentteams.io/v1beta1
kind: Worker
metadata:
  name: demo-dev
  namespace: <ns>
  labels:
    agentteams.io/controller: <controller-name>   # informer 按此 label 过滤，必带
spec:
  runtime: worker-bridge
  adapterMode: cimicode-pod        # 空=规范化 cimicode-pod；另一形态 cimicode-stateless
  workerName: demo-dev
  displayName: demo-dev
  model: native-config             # 用镜像内置 provider（opencode.json），不出 model 段
  soul: |
    你是 Python 工程师。收到任务先 taskflow ack……
---
apiVersion: agentteams.io/v1beta1
kind: Team
metadata: { name: t-demo, namespace: <ns>, labels: { agentteams.io/controller: <controller-name> } }
spec:
  displayName: 演示小队
  teamName: t-demo
  workerMembers:
    - { name: demo-lead, role: team_leader }
    - { name: demo-dev,  role: worker }
```

leader 用 copaw 运行时（`model: glm-5.3-flash`，可加
`spec.env.COPAW_TOOL_GUARD_ENABLED: "false"` 禁用工具护栏）。

### 7.4 dashboard 侧（**不在本分支**，部署时须知）

dashboard 的 worker-bridge 支持（运行时下拉 + adapterMode 字段）目前以
**fork 补丁**形式存在于 105 测试资产 `worker-bridge-105-test/dashboard-fork-patch3.py`
（4 处 assert-then-replace：agentteams-api.ts 加 'worker-bridge' 与 adapterMode、
runtime-meta.ts 补条目、worker-create-dialog.tsx 运行模式下拉、workers-section.tsx
删 opencode→omit 残留）。**合入主干前需把该补丁转正进 dashboard 仓库**。
另：dashboard 调 controller API 需部署层 wiring（admin SA + 投影 token +
`AGENTTEAMS_AUTH_TOKEN_FILE`），见测试资产 dashboard-cimicode-test.yaml 范本。

### 7.5 已知限制

- cimicode-pod 会话数据在 emptyDir：pod 重建即丢——由 adapter 404 自愈 +
  每 turn 重推 agent.md + taskflow mc pull 恢复兜底（设计内；PVC 生产化记
  contract/adapter-contract.md known limitations）。
- Team 级 runtime config 所有权：成员 LLM 配置在 Team 创建时生成存 MinIO，
  改配置需连 Team 一起重建（只重建 Worker 不刷新）。
- Matrix m.file 附件不透传给 bridge worker（只文件名进 turn）；文件协作走
  MinIO 共享（leader 中继已实测全通）。
- DM 直发文件给 worker（不经 leader）是 bridge feature gap。
- 扩编后 copaw leader 组织视图不刷新（copaw 长驻进程不热加载 AGENTS.md，
  需重启 leader pod；worker-bridge worker 无此问题）。

---

## 八、分支状态与遗留事项

- 工作区 clean，与 origin 同步（da22acf4 已推送）。
- 测试分支 `test/worker-bridge-105` = feature + 105 测试资产 + minio
  service.type（§5.7，**有意保留**，勿盲目合并回 feature）。
- 105 测试资产在仓库外 `worker-bridge-105-test/`（team yaml/operator 部署/
  dashboard 补丁/验收记录），按方案不进功能分支。
- 待办建议：① dashboard fork 补丁转正（§7.4）；② minio service.type 是否
  回迁待决策（§5.7）；③ bridge 侧 DM 附件透传（低优先）；④ copaw 侧三个
  工具缺口（delegate_task 容错/projectflow 标记 action/taskflow deliverables
  nargs）。
