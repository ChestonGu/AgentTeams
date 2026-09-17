# worker-bridge pod 模式与 operator 部署详解

> 适用版本：`dev-v1.2.3-cimi`（六提交统一形态改造完成后）。
> 本文回答两个问题：**opencode pod / cimicode pod 到底长什么样、怎么跑起来的**；
> **operator 怎么部署、怎么生效**。全部内容以 2026-09-16 105 环境（cimicode-test
> ns）实测为准，代码引用带行号可点。

---

## 0. 先澄清命名：105 上没有两种 runtime pod 并存

资源命名沿用 `<worker>-cimicode` / `<worker>-cimicode-svc` / `<worker>-cimicode-fs`
（Deployment / Service / Secret），**名字里的 "cimicode" 只是历史命名**——它跑的
镜像由 operator 的 `CIMICODE_IMAGE` 决定，指谁就是谁的形态：

```
105 当前:  cm-dev-cimicode / cm-qa-cimicode 两个 Deployment
           image: agentteams/opencode-runtime:v1.2.3-cimi   ← cimicode 形态的外网模拟件
内网将来:  同名资源，image 换 agentteams/cimicode-runtime:<tag>  ← cimicode 本体（内网生产）
```

cimicode-runtime 是本体，opencode-runtime 是 cimicode 形态的**同契约模拟件**
（外网无内网 registry 条件时的替代；
[opencode-runtime/Dockerfile](../opencode-runtime/Dockerfile) 与
[../cimicode-runtime/Dockerfile](../cimicode-runtime/Dockerfile)），对外行为完全
一致。**内外网切换 = 换 `CIMICODE_IMAGE` 一个值，bridge 与 operator 代码零差异。**
契约权威文本见 [../contract/adapter-contract.md](../contract/adapter-contract.md) v1.1。

105 上跑不了 cimicode-runtime 的原因：其 `ARG CIMICODE_BASE_IMAGE` 指内网
registry 的 coder-cimicode 基础镜像，外网不可达；Makefile 对空值 fail-loud
（Makefile:242）。这本身是验证过的设计点。

---

## 1. pod 拓扑总览

一个 worker（runtime=worker-bridge、adapterMode=cimicode-pod）= **两个 pod + operator 半边**：

```
                        ┌─────────────────────────────────────────────┐
                        │  worker-bridge-operator（ns 级单实例）        │
                        │  watch Worker CR → 供给下方 runtime pod       │
                        └──────────────┬──────────────────────────────┘
                                       │ 创建/漂移纠正
                    ┌──────────────────▼───────────────────┐
  matrix 消息       │  Deployment cm-dev-cimicode           │
  (synapse room)   │  image: agentteams/opencode-runtime    │
       ▲           │  单容器，端口 4096，无卷               │
       │           └──────────────────▲───────────────────┘
       │ poll/send                     │ Service cm-dev-cimicode-svc
  ┌────┴──────────────┐   REST         │
  │ agentteams-worker- │───────────────┘
  │ cm-dev-bridge      │  POST /session/{id}/message
  │ (cimicode-bridge   │  body {"system": agent.md, "parts":[...]}
  │  镜像，matrix 长轮询│
  └────────────────────┘
```

参与协作的固定设施（controller 编排，与本文改造无关但链路相关）：
synapse（room 消息）、MinIO（任务/文件协作存储）、higress 网关（模型入口）。
lead（cm-lead，copaw 镜像）不走 runtime pod 形态，但模型注入同网关。

---

## 2. 共用机制：一套 bridge + 一套 operator，两键指向目标

### 2.1 bridge 侧

bridge pod（`agentteams-worker-<w>-bridge`，cimicode-bridge 镜像 v1.2.3-cimi）
通过 operator patch 的**两个 env** 知道该把 turn 发给谁
（[worker_bridge_operator.py:570 `ensure_worker_env`](../operator/worker_bridge_operator.py#L570)）：

```yaml
BRIDGE_RUNTIME_ADAPTER: cimicode-pod          # 选 pod 适配器（REST 契约）
BRIDGE_RUNTIME_BASE_URL: http://cm-dev-cimicode-svc:4096
```

适配器实现 [../bridge/src/cimicode_bridge/runtime/cimicode_pod_adapter.py](../bridge/src/cimicode_bridge/runtime/cimicode_pod_adapter.py)
对 opencode-runtime / cimicode-runtime 无感知——两者 REST 契约相同。

### 2.2 operator 侧

operator（`deploy/worker-bridge-operator`，单实例）env `CIMICODE_IMAGE` 是唯一
形态开关（[deploy/operator.yaml:84-85](../operator/deploy/operator.yaml#L84-L85)）：

```yaml
- name: CIMICODE_IMAGE
  value: REPLACE_CIMICODE_IMAGE   # 105: agentteams/opencode-runtime:v1.2.3-cimi
                                  # 内网: <registry>/agentteams/cimicode-runtime:<tag>
```

operator 的供给模型（[worker_bridge_operator.py:640 `reconcile_worker`](../operator/worker_bridge_operator.py#L640)）：

| spec.adapterMode | 行为 |
|---|---|
| `cimicode-stateless` | 零供给（绑定走 runtime.yaml bridge 段） |
| `cimicode-pod` / `empty` | 单 runtime Deployment + Service + Secret（FS 凭据 + 渲染的模型配置）+ Worker CR spec.env 两键接线 |

---

## 3. runtime pod 形态（无卷分层）

Deployment 里**没有任何 `volumes:` / `emptyDir`**（operator
[worker_bridge_operator.py:369 `cimicode_deployment`](../operator/worker_bridge_operator.py#L369)
不再声明卷；漂移比较 `deployment_drift` :513 也不含卷）。存储分三层：

| 数据 | 位置 | pod 重建后 |
|---|---|---|
| 代码 / 工作区 | `/workspace`（容器可写层，WORKDIR） | 丢失 → 重新拉取（shared pull） |
| 会话 DB（opencode session） | 容器内（`~/.local/share/opencode`） | 丢失 → bridge POST 遇 404 **重建 session 自愈** |
| 任务态 / 共享文件 / agent.md 归档 | **MinIO 对象存储**（`AGENTTEAMS_FS_*`） | 不丢（持久层） |
| 全局配置种子 | `~/.config/opencode/opencode.json` | entrypoint 每次重种（**存在则不覆盖**） |

配置种子即镜像内 [../opencode-runtime/opencode.json](../opencode-runtime/opencode.json)：

```json
{
  "$schema": "https://opencode.ai/config.json",
  "autoupdate": false,
  "skills": { "paths": ["/opt/agentteams/skills"] }
}
```

零凭据、零 provider——模型配置运行期由 operator 注入（见 §5）。

### 3.1 REST 契约（bridge ↔ runtime，单端口 4096）

- `POST /session` → 会话对象（顶层 `id`）
- `POST /session/{id}/message` body `{"system": <agent_md>, "parts": [{"type":"text","text":...}]}`
  —— **服务端阻塞整 turn**（一次 agentic loop 完成才返回；bridge 给独立长超时，
  超时放弃 ≠ turn 死亡，仍可轮询，见 [cimicode_pod_adapter.py:262-264](../bridge/src/cimicode_bridge/runtime/cimicode_pod_adapter.py#L262-L264) 与 :296-299）
- `GET /session/{id}/message` → 轮询 `info.time.completed` / `error`

### 3.2 agent.md 逻辑（helper 链路已退役）

1. bridge **每 turn** 调 `/opt/agenttools/generate_agent_md.py` 生成 agent.md
   （~11KB：identity + 协作协议 + skills 清单；实测 cm-dev 11038 / cm-qa 11108 字节）
2. 随 POST body 的 **`system` 字段**下发（opencode 1.18.27
   `PromptInput.system` 通道，cimicode 同型）——**不再写 AGENTS.md 文件、不再有
   helper 端口**
3. **每 turn 独立注入**：同会话第二 turn 只认新 system（S1 冒烟双标记验证过），
   无重复注入
4. bridge 同时写一份 `s3://agentteams-storage/agents/<w>/agent-md/latest.md`
   ——仅供 dashboard 展示，runtime 不消费

---

## 4. 镜像内目录结构（pod 实录）与构建打包

### 4.1 目录结构

```
/opt/agentteams/
├── entrypoint.sh          # 种配置 + exec serve（CRLF 防护）
├── opencode.json          # 种子配置（上文）
└── skills/                # ← 源自仓库 worker-bridge/template/worker-bridge-agent/skills
    ├── communication/     # mention / 汇报协议
    ├── file-sharing/      # shared push/pull 用法
    ├── mcporter/          # MCP 桥
    ├── organization/      # 团队拓扑认知
    └── task-management/   # taskflow 用法（含 DAG 执行参考）
/usr/local/bin/
├── taskflow               # 任务协作协议 CLI（ack/submit/状态机，python3 stdlib）
├── agentteams-sync        # 文件同步 CLI（MinIO push/pull）
└── mc                     # vendored MinIO client（worker-bridge/bin/mc）
/root/.config/opencode/
└── opencode.json          # entrypoint 种下的运行配置（skills.paths 指向镜像内目录）
```

**生效机制**：种子配置 `skills.paths=["/opt/agentteams/skills"]` → opencode
**原生 skill 系统**直读镜像内目录（零拷贝、无运行时下载）。skill md 教模型调
`taskflow` / `agentteams-sync` 这两个 shell 命令。实测行为：cm-dev "读取
file-sharing 技能以正确发布项目文件"、cm-qa "读 communication 技能后通知协调者"。

CLI 包装器在镜像构建期生成（[opencode-runtime/Dockerfile:72-75](../opencode-runtime/Dockerfile#L72-L75)）：

```dockerfile
RUN printf '#!/bin/sh\nexec python3 /opt/agentteams/skills/task-management/scripts/taskflow.py "$@"\n' > /usr/local/bin/taskflow \
    && printf '#!/bin/sh\nexec python3 /opt/agentteams/skills/file-sharing/scripts/agentteams_sync.py "$@"\n' > /usr/local/bin/agentteams-sync
```

### 4.2 构建打包（Makefile，构建上下文=仓库根）

```makefile
LOCAL_CIMICODE_RUNTIME  = agentteams/cimicode-runtime:$(VERSION)    # :59
LOCAL_OPENCODE_RUNTIME  = agentteams/opencode-runtime:$(VERSION)    # :60
```

| 目标 | 说明 |
|---|---|
| `make build-opencode-runtime` | node:22-slim + apt(ripgrep/python3/curl/jq/git/procps) + `npm i -g opencode-ai@1.18.27`（钉版）+ COPY skills/mc/entrypoint。**构建-arg**：`OPENCODE_VERSION`、`NPM_REGISTRY` |
| `make build-cimicode-runtime` | FROM `$(CIMICODE_BASE_IMAGE)`（内网 coder-cimicode，bun 二进制内置无 npm 步骤）。**必须**传 `CIMICODE_BASE_IMAGE=<internal-registry>/library/coder-cimicode:0.5.0`，空值直接报错退出（Makefile:242 fail-loud） |
| `make build-cimicode-bridge` / `build-worker-bridge-operator` | bridge / operator 镜像，与 runtime 无耦合 |

105 实测三镜像体量：opencode-runtime 1.1GB / cimicode-bridge 287MB /
worker-bridge-operator 284MB（`VERSION=v1.2.3-cimi`）。

两镜像目录内部差异（全部收敛在镜像内，契约外不可见）：

| | opencode-runtime | cimicode-runtime |
|---|---|---|
| 基础镜像 | node:22-slim + npm | coder-cimicode（bun 编译单二进制） |
| 配置路径 | `~/.config/opencode/opencode.json` | `~/.cimi/cimicode/cimicode.json` |
| 启动命令 | `opencode serve --port $OPENCODE_PORT --hostname 0.0.0.0` | `cimicode serve` |
| ripgrep/python3 | apt 补装（node-slim 缺 rg，缺则 skill 扫描挂 130s） | 基础镜像已含 |

---

## 5. 模型注入链路（operator 的核心职责）

**统一网关注入，全链零凭据镜像**。leader（copaw）与 worker 走同一条路：

```
controller（无条件注入 bridge pod env）
  AGENTTEAMS_AI_GATEWAY_URL = http://higress-gateway:80
  AGENTTEAMS_WORKER_GATEWAY_KEY = <per-worker>
        ↓ operator 读到
render_model_config（worker_bridge_operator.py:314）
  生成 cimicode 方言 JSON：
    provider "agentteams-gateway"（@ai-sdk/openai-compatible）
    baseURL = <gateway_url>/v1（归一化只在此处：strip + rstrip("/")）
    models: { "<spec.model>": {...} }
        ↓
Secret <w>-cimicode-fs，key=model-config（与 FS 凭据同 Secret，三 key）
        ↓ deployment env
OPENCODE_CONFIG_CONTENT        ← secretKeyRef（runtime 最高优先级配置通道）
CIMICODE_MODEL_CONFIG_HASH     ← 明文 sha256[:16]（如 cdaea75b793ced2d）
        ↓ 哈希变更 → _env_fingerprint 变化 → deployment_drift → 整 spec replace 滚动
runtime pod 读 OPENCODE_CONFIG_CONTENT → 请求网关
        ↓
higress（controller 调 console admin API 建的 modifyAI routes：consumer + 授权路由）
        ↓ 真实智谱 key 只在网关 upstream
```

特殊路径：`spec.model` 为 `native-config`（大小写/空白不敏感）→ 跳过注入、
不建 Secret（保留 worker 自带配置）；spec.model 为空或缺网关 env → **推迟供给**
（不动已有栈，日志报齐缺失项，`model_config_content` :280 三态逻辑）。

实测：session 内 `model=glm-5.3-flash, providerID=agentteams-gateway`，单 session
cache read 512k tokens，真实调通。

---

## 6. runtime pod 环境变量注入清单（三来源）

105 上 `kubectl get deploy cm-dev-cimicode -o yaml` 实录，按来源分层：

| 来源 | 变量 | 值 / 方式 |
|---|---|---|
| **controller**（worker Deployment 模板注入） | `AGENTTEAMS_MATRIX_USER_ID` | `@cm-dev:<matrix-domain>`（明文） |
| | `AGENTTEAMS_TEAM` | `t-cimi` |
| | `AGENTTEAMS_WORKER_NAME` | `cm-dev` |
| | `AGENTTEAMS_FS_ROOT` | `/workspace` |
| | `AGENTTEAMS_FS_ENDPOINT` | `http://agentteams-cimicode-minio.<ns>.svc.cluster.local:9000` |
| | `AGENTTEAMS_FS_BUCKET` | `agentteams-storage` |
| | `AGENTTEAMS_FS_ACCESS_KEY` / `_SECRET_KEY` | secretKeyRef（MinIO 凭据） |
| **operator**（模型注入两键） | `OPENCODE_CONFIG_CONTENT` | secretKeyRef → Secret `<w>-cimicode-fs` key=`model-config` |
| | `CIMICODE_MODEL_CONFIG_HASH` | 明文 sha256[:16] |
| **镜像 ENV**（Dockerfile 固化） | `OPENCODE_PORT` | `4096` |
| | `OPENCODE_PERMISSION` | `{"*":"allow"}`（headless 全放行） |
| | `AGENTTEAMS_SKILLS_ROOT` | `/opt/agentteams/skills` |

**禁止**设置 `AGENTTEAMS_RUNTIME`（mc 同步会误走 k8s 模式；本 pod 的契约是
local 三元组 FS_ROOT/ENDPOINT/BUCKET，见 Dockerfile 头注释）。

bridge pod 上另有两组：controller 注的网关两键
（`AGENTTEAMS_AI_GATEWAY_URL` + `AGENTTEAMS_WORKER_GATEWAY_KEY`）与 operator
patch 的 runtime 两键（`BRIDGE_RUNTIME_ADAPTER` / `_BASE_URL`）。

---

## 7. operator 部署与生效

### 7.1 资产与实例的关系

- **仓库模板**：[../operator/deploy/operator.yaml](../operator/deploy/operator.yaml)
  ——`REPLACE_NS` / `REPLACE_IMAGE` / `REPLACE_CIMICODE_IMAGE` /
  `REPLACE_MINIO_SVC` 占位符，含完整 RBAC（workers/teams/deployments/pods/
  services/secrets）
- **105 实例**：`~/worker-bridge-build/operator-105.yaml`（模板的实例化 delta）：
  - `namespace: cimicode-test`（含 RBAC 三件套同 ns）
  - `image: agentteams/worker-bridge-operator:v1.2.3-cimi`
  - `CIMICODE_IMAGE: agentteams/opencode-runtime:v1.2.3-cimi` ← 形态开关
  - `AGENTTEAMS_FS_ENDPOINT` 指向 cimicode-test 的 MinIO svc
  - 额外 `nodeSelector` 钉节点（105 单机多 ns 隔离用，可选）
  - `WATCH_NAMESPACE=cimicode-test`、`RECONCILE_INTERVAL=10`（秒）、
    `CIMICODE_PROBE_PATH=/session`（就绪探针，空则禁用）

### 7.2 部署序列（105 实操，可照抄）

```bash
# 1) 构建上下文：仓库快照同步到 105（git archive，不用 .git）
#    105: ~/worker-bridge-build/src/
cd ~/worker-bridge-build/src

# 2) 构建三镜像（VERSION 决定 tag）
make VERSION=v1.2.3-cimi build-opencode-runtime build-cimicode-bridge build-worker-bridge-operator

# 3) 导入 containerd（k3s 不读 docker daemon！）
#    ⚠️ 必须 docker save 到 tar 文件再 import——管道方式曾静默失败
docker save -o /tmp/opencode-runtime.tar agentteams/opencode-runtime:v1.2.3-cimi
sudo k3s ctr -n k8s.io images import /tmp/opencode-runtime.tar
#   bridge/operator 两个镜像同理；逐个核对：
sudo k3s ctr -n k8s.io images ls | grep v1.2.3-cimi

# 4) apply operator（先镜像后 apply，见 §8 GC 坑）
kubectl apply -f ~/worker-bridge-build/operator-105.yaml

# 5) bridge 镜像版本由 controller 决定（它创建 bridge pod）：
kubectl -n cimicode-test set env deploy/agentteams-cimicode-controller \
  AGENTTEAMS_WORKER_BRIDGE_IMAGE=agentteams/cimicode-bridge:v1.2.3-cimi
```

### 7.3 生效验证

```bash
# operator 起机横幅（确认 ns/镜像/端口/间隔）
kubectl -n cimicode-test logs deploy/worker-bridge-operator | head
#   worker-bridge-operator starting ns=cimicode-test
#   cimicode=agentteams/opencode-runtime:v1.2.3-cimi port=4096 interval=10s

# 每个 cimicode-pod worker 一行（model=injected 是模型注入成功的标志）
kubectl -n cimicode-test logs deploy/worker-bridge-operator --tail=10
#   worker cm-dev reconciled (mode=cimicode-pod svc=cm-dev-cimicode-svc team=t-cimi fs=secret model=injected)

# runtime pod 内自证
kubectl -n cimicode-test exec cm-dev-cimicode-<hash> -- \
  curl -s localhost:4096/session | head -c 200
```

worker / team CR 由 controller 消化（`runtime: worker-bridge`、
`adapterMode: cimicode-pod`、`model: glm-5.3-flash`），operator 只认 Worker CR，
不直接创建——所以"加一个 worker"不需要碰 operator。

### 7.4 升级生效路径（重要）

- **换 runtime 形态**（内网 cimicode ↔ 外网 opencode）：set operator 的
  `CIMICODE_IMAGE` → operator 下一轮 reconcile 对 Deployment 整 spec replace
  滚动（svc 端口漂移则 patch）
- **换模型**：改 Worker CR `spec.model` → operator 重渲染 model-config →
  `CIMICODE_MODEL_CONFIG_HASH` 变化 → 同样整 spec replace 滚动
- **升 operator 自身**：apply 新 operator.yaml（镜像先导入，见 §8）

---

## 8. 坑与纪律（105 实测踩过）

1. **升级序禁令：禁止先换 runtime 镜像后升 operator**。新镜像零凭据，旧
   operator 不注入模型 env → runtime 起来后每个 turn 全失败。正确序：
   runtime 镜像导入 → operator 升级 → controller 侧 bridge 同窗口升。
2. **containerd GC 坑**：磁盘高水位（84%）时，清场空窗期未被 pod 引用的镜像
   会被驱逐。**先 import 后 apply**；重建前必重导。管道 import 会静默失败，
   一律 tar 文件方式并逐个核对 `ctr images ls`。
3. **存量 Worker CR 残留 `BRIDGE_RUNTIME_HELPER_URL`**：旧键，新 bridge 不读，
   operator 会显式清掉（一轮 reconcile 收敛），无害。
4. **CRLF**：Windows 编辑过的 entrypoint.sh 必须过 `sed -i "s|\r$||"`（镜像构建
   已内置），否则 `exec` 报 no such file or directory。
5. **`AGENTTEAMS_RUNTIME` 不得设**（见 §6）。
6. **copaw re-bridge bug（预存在）**：controller 推配置变化（team 成员增减 /
   set env 滚动）会触发 copaw `FileSync.get_soul` 死路径，删 cm-lead pod 重建
   即恢复；初始 bridge 不走该路径。与本次改造无关，观察中。

---

## 9. 已知差异与限制

1. 资源命名 `<w>-cimicode*` 固定，跑 opencode-runtime 时也不改名（认知注意）。
2. 无卷 → pod 重建丢会话（bridge 404 自愈重建 session 兜底；任务态在 MinIO 不丢）。
3. cimicode 0.5.0 与 opencode 1.18.27 的 system 字段语义分别只在各自形态冒烟过
   （1.18.27 侧 S1/S2 全过；cimicode 侧待内网冒烟）。
4. 105 不具备 cimicode-runtime 构建条件（内网 registry），该镜像只在契约与
   Makefile 层面验证（守卫 fail-loud 本身是验证点）。

---

## 附录 A：2026-09-16 回归实测证据（105，t-cimi 全流程）

- 五场景全过：lead 介绍核对 → projectflow 拆解委派（DAG 实现→验收）→
  cm-dev taskflow ack/实现/submit（turn 290s，21 单测，lead 抽查复跑 21/21）→
  cm-qa 独立验收 PASS（turn 403s，二次 submit 被协议状态机正确拦截）→
  项目完结报告
- system 每 turn 注入：bridge 日志 `post message ... system_bytes=11038/11108`
  多 turn 各注入
- 群聊视野 buffer：cm-qa 单 turn 输入打包"自上次回复以来"全部消息（增量，
  不回放全历史）
- 模型链路：session `model=glm-5.3-flash providerID=agentteams-gateway`，
  cache read 512k tokens
- 健康：6 pod 0 重启，operator 0 错误，两 bridge 0 ERROR/CRITICAL
