# worker-bridge — AgentTeams 的 cimicode 形态 worker 运行时

把 AgentTeams 里 copaw 形态的 worker 替换为 **cimicode 形态运行时**，同时保持协作
协议完全同型（taskflow 任务协作、MinIO `shared/` 文件协作、群聊 mention 触发、
leader projectflow 拆解委派）。**cimicode 是本体**（内网生产形态，换皮 opencode，
bun 编译单二进制），分 **stateless / pod 两种模式**（§2）；外网没有内网 registry
条件，用 npm 包 `opencode-ai` 钉 1.18.27 构建**同契约模拟件**（opencode-runtime，
仅在外网开发/测试时替代本体）——bridge/operator 单一代码零差异，内外网切换只换
`CIMICODE_IMAGE` 一个值。

设计总原则（D0）：一切以 copaw 机制为准，唯一改变是工具载体。

## 1. 形态总览

三个组件，每 worker 一套：

```
                Matrix (Synapse) team room
                       ▲   │ mention（m.mentions 结构化）
          回复 + progress │   ▼
     ┌───────────────────────────────────────┐
     │ bridge pod（伪装 worker 的 Matrix 身份）│  agentteams/cimicode-bridge
     │  matrix 防护 / turn 编排 / 自愈轮询     │  （controller 创建）
     │  generate_agent_md.py → system prompt  │
     └───────────────────┬───────────────────┘
                         │ BRIDGE_RUNTIME_ADAPTER=cimicode-pod
                         │ BRIDGE_RUNTIME_BASE_URL=http://<w>-cimicode-svc:4096
                         ▼
     ┌───────────────────────────────────────┐
     │ runtime pod（operator 供给）            │  cimicode-runtime（本体）/
     │  cimicode serve :4096（单端口 REST）   │  opencode-runtime（外网模拟件）
     │  taskflow / agentteams-sync / mc +     │
     │  skills 全套镜像预装                    │
     └───────────────────┬───────────────────┘
                         │ 任务态 / 文件协作（mc → MinIO）
                         ▼
                  teams/{team}/shared/
```

- **bridge pod**：controller 消化 Worker CR（`runtime: worker-bridge`）后创建，持有
  与 worker 相同的 Matrix 身份和 runtime.yaml 投影。会话循环不在它自己身上——它把
  每个 turn 转发给 runtime pod，等待并回传。adapter 裁决顺序：**显式 env 两键 >
  runtime.yaml 顶层 bridge 段 > 未定态**（不建 client，轮询等接线）。
- **operator**（`agentteams/worker-bridge-operator`）：runtime 无关供给器。watch 本
  ns 内 `runtime=worker-bridge` 的 Worker CR，按 `spec.adapterMode` 分派，供给
  runtime Deployment + svc + Secret，并把接线两键 patch 回 Worker CR env。
- **runtime pod**：单容器 `cimicode serve`（:4096；外网模拟件为 `opencode serve`，
  同型），无卷（/workspace=容器可写层，pod 重建丢会话由 bridge 404 自愈重建兜底；
  任务态在 MinIO 不丢）。

## 2. 模式形态：stateless / pod 两种模式，本体与模拟件双镜像

Worker CR `spec.adapterMode`（[types.go:270](../agentteams-controller/api/v1beta1/types.go#L270)）：

| adapterMode | 形态 | 供给物 |
|---|---|---|
| `cimicode-pod`（含空值默认） | operator 供给单 runtime pod，bridge 经 svc 直连 | Deployment `<w>-cimicode` + svc `<w>-cimicode-svc`（单端口 :4096）+ Secret `<w>-cimicode-fs`（FS 凭据 + model-config） |
| `cimicode-stateless` | bridge 直调外部 cimicode 平台（绑定四字段 `cimicodeGatewayUrl`/`sessionId`/`sandboxId`/`templateId`，controller 投影进 runtime.yaml 顶层 bridge 段） | 零供给 |

**同一形态的两个镜像实现**（对外契约完全同一，见
[contract/adapter-contract.md](contract/adapter-contract.md) v1.1）：

| 目录 | 基础 | 角色 |
|---|---|---|
| `cimicode-runtime/` | 内部 coder-cimicode 基础镜像（`CIMICODE_BASE_IMAGE` 构建必填，内网 registry 地址不进外网仓库，Makefile 空值守卫 fail-loud） | **本体**（内网生产） |
| `opencode-runtime/` | node:22-slim + npm `opencode-ai@1.18.27`（apt 补 ripgrep/python3） | **模拟件**（外网无内网镜像条件时开发/测试用） |

切换只改 operator 的 `CIMICODE_IMAGE` env，下一轮 reconcile 对 Deployment 整
spec replace 滚动。资源命名 `<w>-cimicode*` 固定，跑 opencode-runtime 时也不改名。

## 3. 消息与任务流转

### 3.1 一条消息的生命周期

1. 房间 mention（`m.mentions` 结构化 + matrix.to formatted_body）→ Synapse 推给
   bridge（同 worker 身份）。
2. bridge 编排 turn：调 `bridge/generate_agent_md.py` 生成 system prompt——源模板
   （`template/worker-bridge-agent/AGENTS.md`，骨架固化 + 仅
   `{{COORDINATION}}`/`{{ENVIRONMENT}}` 两占位符）+ runtime.yaml 结构化渲染
   （PyYAML safe_load）+ SOUL/PROFILE 逐字合并，fail-loud。**每个 turn 现生成**，
   canonical AGENTS.md 已彻底退役（架构 v2.4）。
3. adapter 按裁决顺序选中 `cimicode-pod`，POST `/session` 懒建会话 →
   POST `/session/{id}/message`，body `{"system": <agent_md>, "parts": [{"type":
   "text","text": <用户消息>}]}`——**服务端阻塞整 turn**，bridge 侧独立长超时
   （默认 600s+30s，[cimicode_pod_adapter.py:50](bridge/src/cimicode_bridge/runtime/cimicode_pod_adapter.py#L50)）。
4. bridge 期间轮询 session 消息拿增量；turn 完成（`info.time.completed`/`error`）
   后：先发 turn 中途的 progress 插话，再发正式回复到房间。POST 超时≠turn 死亡
   （会话态完好可再问），失败/断连上报 `turn failed`。
5. runtime pod 内 agent 执行工具：taskflow / agentteams-sync / mc（镜像预装
   `/usr/local/bin`），skills 镜像预装 `/opt/agentteams/skills` 经
   `skills.paths` 直读原生生效。所有工具日志统一写一个 JSONL
   （`$AGENTTEAMS_FS_ROOT/logs/agentteams.log`，契约 §5.5）。

### 3.2 任务与文件协作（与 copaw 同型）

- worker 侧 `taskflow`（check/ack/submit）→ mc → MinIO `teams/{team}/shared/`；
  leader 侧 `projectflow`（plan-dag/delegate/check，推送无排除=协议所有方）。
- 同步根 `/workspace`（pull/push/stat/list），push 恒 exclude `spec.md`+`base/`。

### 3.3 模型注入链（镜像零凭据）

```
Worker spec.model + bridge pod 网关两 env（AGENTTEAMS_AI_GATEWAY_URL /
AGENTTEAMS_WORKER_GATEWAY_KEY，controller 注入）
  → operator render_model_config 渲染 agentteams-gateway provider JSON
  → Secret <w>-cimicode-fs key=model-config
  → runtime pod env OPENCODE_CONFIG_CONTENT（secretKeyRef）
    + CIMICODE_MODEL_CONFIG_HASH（明文 sha256[:16]，变更触发整 spec replace 滚动）
```

`native-config` 模型跳过注入；`spec.model` 空或网关 env 缺失 → 本轮零副作用推迟
（旧栈保留，下一轮重试）。

### 3.4 env 三来源（禁设 `AGENTTEAMS_RUNTIME`）

| 来源 | env | 作用 |
|---|---|---|
| controller → bridge pod | `AGENTTEAMS_MATRIX_USER_ID` / `_TEAM` / `_WORKER_NAME`、`AGENTTEAMS_FS_ROOT`/`_ENDPOINT`/`_BUCKET`/`_ACCESS_KEY`/`_SECRET_KEY`、`AGENTTEAMS_AI_GATEWAY_URL` + `AGENTTEAMS_WORKER_GATEWAY_KEY` | 身份 / 协作存储 / 模型网关 |
| operator → Worker CR env（最终落 bridge pod） | `BRIDGE_RUNTIME_ADAPTER` / `BRIDGE_RUNTIME_BASE_URL` | 接线两键（残留 HELPER 键显式清除） |
| operator → runtime pod | `OPENCODE_CONFIG_CONTENT`（secretKeyRef）、`CIMICODE_MODEL_CONFIG_HASH` | 模型注入 |
| 镜像 ENV → runtime pod | `OPENCODE_PORT=4096`、`OPENCODE_PERMISSION={"*":"allow"}`、`AGENTTEAMS_SKILLS_ROOT=/opt/agentteams/skills` | 服务端口 / 权限 / skills |

## 4. 目录结构

| 目录 | 内容 |
|---|---|
| `bridge/` | bridge 进程本体（`src/` FastAPI：matrix 防护、turn 编排、adapter 裁决、自愈轮询）+ `generate_agent_md.py`（agent.md 生成工具）+ `agentteams_log.py`（统一日志）+ `tests/` |
| `operator/` | worker-bridge-operator（runtime 无关供给器，watch Worker CR 分派供给/推迟/GC）+ `deploy/operator.yaml` 模板 + `tests/`（24 用例） |
| `cimicode-runtime/` | cimicode 本体镜像构建上下文（内网 coder-cimicode 基础镜像） |
| `opencode-runtime/` | cimicode 形态的外网模拟件（node:22-slim + npm opencode-ai@1.18.27，同契约） |
| `bin/` | vendored mc 二进制（两 runtime Dockerfile 共享 COPY 源） |
| `template/worker-bridge-agent/` | worker 模板（AGENTS.md 源模板 + 5 skills + scripts 部署副本） |
| `template/worker-bridge-leader-agent/` | leader 模板 starter（leader 版 skill + projectflow 三件套，预置未部署） |
| `cli/taskflow/` | taskflow CLI（worker 侧 check/ack/submit）+ mc 同步后端 + 统一日志模块 |
| `cli/sync/` | agentteams-sync CLI（pull/push/stat/list） |
| `cli/projectflow/` | projectflow CLI（leader 侧，core=copaw task.py 全量 vendor） |
| `contract/` | `interface-contract.md` v2.4（协作契约）/ `controller-handover.md` / `adapter-contract.md` v1.1（统一形态传输契约） |
| `cimicode-sandbox/` | stateless 形态 cimicode 自用沙箱的基础镜像 |
| `docs/` | 设计与操作文档（见 §8 索引） |

冒烟/模拟器等测试资产（`verify/`、smoke 记录）留在源分支
`dev-v1.2.2-opencode-ben-test` 可追溯，不随本目录搬运。

## 5. 部署与使用

### 5.1 构建镜像（仓库根 Makefile）

```bash
make VERSION=v1.2.3-cimi CIMICODE_BASE_IMAGE=<内网registry>/coder-cimicode:x build-cimicode-runtime
                                                     # 本体：内网生产镜像（BASE_IMAGE 空则守卫 fail-loud）
make VERSION=v1.2.3-cimi build-opencode-runtime      # 模拟件：外网验证契约用（agentteams/opencode-runtime）
make VERSION=v1.2.3-cimi build-cimicode-bridge       # bridge（agentteams/cimicode-bridge，上下文=仓库根）
make VERSION=v1.2.3-cimi build-worker-bridge-operator # operator（agentteams/worker-bridge-operator）
```

### 5.2 部署 operator

模板 [operator/deploy/operator.yaml](operator/deploy/operator.yaml)，四个占位符：
`REPLACE_NS` / `REPLACE_IMAGE`（operator 镜像）/ `REPLACE_CIMICODE_IMAGE`
（**形态开关**：指 cimicode-runtime 或 opencode-runtime）/ `REPLACE_MINIO_SVC`。
关键 env：`WATCH_NAMESPACE`、`CIMICODE_PORT=4096`、`CIMICODE_PROBE_PATH=/session`
（就绪探针，空则禁用）、`AGENTTEAMS_FS_ENDPOINT`/`_BUCKET`、`RECONCILE_INTERVAL`。
含完整 RBAC（workers/teams/deployments/pods/services/secrets）。

k3s 环境导入（docker daemon 镜像 k3s 不读；**管道 import 会静默失败，必须 tar 文件**）：

```bash
docker save -o /tmp/opencode-runtime.tar agentteams/opencode-runtime:v1.2.3-cimi
sudo k3s ctr -n k8s.io images import /tmp/opencode-runtime.tar   # 逐镜像同理，ctr images ls 核对
kubectl apply -f operator-105.yaml
```

bridge 镜像版本由 controller 决定（它创建 bridge pod）：

```bash
kubectl -n <ns> set env deploy/agentteams-cimicode-controller \
  AGENTTEAMS_WORKER_BRIDGE_IMAGE=agentteams/cimicode-bridge:v1.2.3-cimi
```

**升级序禁令：禁止先换 runtime 镜像后升 operator**——新镜像零凭据，旧 operator
不注入模型 env，runtime 起来后每个 turn 全失败。正确序：runtime 镜像导入 →
operator 升级 → controller 侧 bridge 同窗口升。

### 5.3 创建 Worker / Team CR（105 实测形态）

```yaml
apiVersion: agentteams.io/v1beta1
kind: Worker
metadata:
  name: cm-dev
  namespace: cimicode-test
spec:
  runtime: worker-bridge        # 必填，operator 只 watch 这个值
  adapterMode: cimicode-pod     # 空=同 cimicode-pod；stateless 见 §2
  model: glm-5.3-flash          # 必填（无 omitempty）；空则 operator 推迟供给
  soul: |
    你是一名后端开发工程师，……
---
apiVersion: agentteams.io/v1beta1
kind: Team
metadata:
  name: t-cimi
  namespace: cimicode-test
spec:
  workerMembers:                # 引用已有 Worker CR；controller 建房+注入运行上下文
    - { name: cm-lead, role: team_leader }
    - { name: cm-dev, role: worker }
```

controller 消化 CR（建房、身份、runtime.yaml 投影、bridge pod），operator 只认
Worker CR——**加 worker 不需要碰 operator**。leader 一般仍用 copaw runtime
（`runtime: copaw`），worker-bridge worker 与之在同一房间按同一协议协作。

### 5.4 生效验证

```bash
kubectl -n <ns> logs deploy/worker-bridge-operator | head
#   worker-bridge-operator starting ns=... cimicode=agentteams/opencode-runtime:... port=4096
kubectl -n <ns> logs deploy/worker-bridge-operator --tail=10
#   worker cm-dev reconciled (mode=cimicode-pod svc=cm-dev-cimicode-svc ... model=injected)
kubectl -n <ns> exec cm-dev-cimicode-<hash> -- curl -s localhost:4096/session | head -c 200
```

`model=injected` 是模型链成功的标志。随后在 team room 用 `m.mentions` 结构化
mention 触发一轮 turn 即为端到端验证。

### 5.5 变更生效路径

| 变更 | 操作 | 生效方式 |
|---|---|---|
| 换模型 | 改 Worker CR `spec.model` | operator 重渲染 → hash 变化 → 整 spec replace 滚动 |
| 换形态（内↔外网） | set operator `CIMICODE_IMAGE` | 下一轮 reconcile 整 spec replace 滚动 |
| 升 operator 自身 | 先导入镜像再 apply 新 yaml | Deployment 滚动 |
| 升 bridge | set env controller `AGENTTEAMS_WORKER_BRIDGE_IMAGE` | controller 重建 bridge pod |

逐步实操（含 containerd GC 坑、CRLF 坑、存量 HELPER 键收敛等 105 实测细节）：
[docs/worker-bridge-pod模式与operator部署详解.md](docs/worker-bridge-pod模式与operator部署详解.md) §7-§8。

## 6. 本地验证

```bash
# 单测（共 208 个；开发机需 pip install pyyaml）
cd cli/taskflow && python -m unittest discover -s tests   # 40 个（taskflow + mc_sync + 统一日志）
cd cli/sync && python -m unittest discover -s tests       # 10 个（agentteams-sync）
cd cli/projectflow && python -m unittest discover -s tests # 20 个（leader core + CLI + 与 worker 闭环）
cd bridge && python -m pytest tests -q        # 114 个（进程单测 71 + 生成器 43）
cd ../operator && python -m pytest tests -q    # 24 个（svc/模型注入+哈希 drift/两键/GC/推迟）

# agent.md 生成工具（bridge 调用形态；runtime.yaml/SOUL/PROFILE 从 MinIO 拉下后传路径）
python bridge/generate_agent_md.py --runtime-config <runtime.yaml> \
    [--soul-file SOUL.md --profile-file PROFILE.md]   # stdout 即 system prompt
```

## 7. 与 copaw 的已知差异（有意为之）

1. **CLI 层前置 CAS**：copaw 的 ack/submit 只查身份+room 不查状态（重 ack 会把
   submitted 打回 in_progress）。本 CLI 命令层拒绝：ack 要求 assigned（in_progress
   幂等成功）、submit 要求 in_progress。core 层保持与 copaw 逐行等价，产物协议逐字段一致。
2. **显式 UTF-8 + LF**：core 文件读写显式 `encoding="utf-8"`、写入显式
   `newline="\n"`（Windows 开发机默认 GBK+CRLF 会污染上传产物）。Linux 沙箱下字节一致。
3. **push/verify 失败回滚**：copaw submit 推送失败后本地已 submitted 无回滚；worker
   CLI 恢复命令前快照的 meta.json，leader CLI 快照全部被改协议文件。
4. **projectflow 推送无排除**：相对 worker taskflow 恒 exclude `spec.md`+`base/`——
   leader 是协议所有方，plan/spec/meta 从 leader 推向存储。

## 8. 文档索引

| 文档 | 内容 |
|---|---|
| [contract/adapter-contract.md](contract/adapter-contract.md) | 统一形态传输契约 v1.1（REST 双标定、模型注入、两键接线、已知限制）——**接口权威** |
| [contract/interface-contract.md](contract/interface-contract.md) | 协作契约 v2.4（env/镜像布局/消息/命令/统一日志/agent.md 生成契约） |
| [docs/worker-bridge-pod模式与operator部署详解.md](docs/worker-bridge-pod模式与operator部署详解.md) | pod 模式实测形态 + operator 部署使用全流程 + 105 实测坑——**部署权威** |
| [docs/worker-bridge运行时替换与协作流转详解.md](docs/worker-bridge运行时替换与协作流转详解.md) | 运行时替换设计与协作流转 |
| [docs/worker-bridge-worker运行时迁移方案.md](docs/worker-bridge-worker运行时迁移方案.md) | 迁移史（D0 总原则） |
| [docs/worker-bridge功能分支变更详解（vs dev-v1.2.2.p1）.md](docs/worker-bridge功能分支变更详解（vs dev-v1.2.2.p1）.md) | v1.2.3 前半段（三链路收敛）变更详解 @ da22acf4——统一形态改造**前**的快照，头部注记列明已退役设计 |
| [docs/内网opencode迁移cimicode迁移文档.md](docs/内网opencode迁移cimicode迁移文档.md) | 内网迁移记录（含四外网差异注记） |

本目录随 AgentTeams 主仓库管理（当前开发分支 `dev-v1.2.3-cimi`）。
