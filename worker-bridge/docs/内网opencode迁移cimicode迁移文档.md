# cimicode-runtime 迁移与重放指南（opencode 形态 → cimicode 形态）

> **外网落地注记（2026-09-16，`dev-v1.2.3-cimi` 提交 7b7d64a1…f87d4ef4）**：
> 本文档（内网迁移记录）已在外网仓库重放为**统一形态**，与文档方案的四处差异：
>
> 1. **operator 不分叉**：未新建 `operator-cimicode/`，直接升级
>    `worker-bridge/operator/`——已核实 opencode 1.18.27 具备全部四通道
>    （`PromptInput.system` / `OPENCODE_CONFIG_CONTENT` / `OPENCODE_PERMISSION`
>    / `skills.paths`），bridge 与 operator 对两 runtime 完全同一，内外网
>    切换只换 operator env `CIMICODE_IMAGE` 一个值。
> 2. **`opencode-runtime/` 非备份**：是 cimicode 形态的外网**模拟件**
>    （node:22-slim + npm `opencode-ai@1.18.27`，外网无内网 registry 条件时
>    验证契约用），`cimicode-runtime/` 才是本体；旧 opencode 形态由 git 历史
>    回溯，不做目录存档。
> 3. **vendored mc 已上移 `worker-bridge/bin/mc`**（两 Dockerfile 共享
>    COPY），文档中 `cimicode-runtime/bin/mc` 路径作废。
> 4. **`CIMICODE_BASE_IMAGE` 无默认值 + Makefile 守卫**：外网仓库不含内网
>    registry 地址，构建必须显式传
>    `CIMICODE_BASE_IMAGE=<internal-registry>/library/coder-cimicode:0.5.0`，
>    缺省 fail-loud。
>
> **升级序警告（105 部署）**：①先构建推送新 runtime 镜像 → ②升 operator →
> ③升 bridge（与 operator 同窗口）。**禁止先换镜像后升 operator**——新镜像
> 零凭据，旧 operator 不注入模型 → turn 全失败。模型链缺失（spec.model 空
> 或网关 env 缺）的存量 worker 推迟供给，旧栈原样保留继续跑，补齐后收敛。

> **用途**：在另一份源代码上完整重放本次全部修改（测试代码除外）。
> **基线**：worker-bridge 分支的 opencode 形态（cimicode-runtime = opencode-ai npm 包
> + pod 内 helper + bash.ts + ZHIPU_API_KEY 烘焙；operator = 双端口 + emptyDir）。
> **目标形态**：基于内部 `coder-cimicode:0.5.0` 基础镜像，agent.md 走消息体
> `system` 字段，模型由 operator 从 Worker CR 动态注入，无 helper、无烘焙凭据。
> **cimicode 源码仓库**：`Q:\workspace\cimicode-new\agi-opencode`（本文所有
> "cimicode 源码依据"均指该仓库的 `packages/opencode` / `packages/core`）。
> **脱敏说明**：内部 registry 域名与内网镜像地址已以 `<internal-registry>` /
> "内网镜像源" 占位，实际值见仓库内 Dockerfile / Makefile / 构建环境。

---

## 1. 改动的整体思路与框架

一句话总结：**运行时从"npm 安装的 opencode + 三件自造辅助（helper 落
AGENTS.md、bash.ts 转发、烘焙 provider）"换成"cimicode 官方镜像 + 原生
通道"**——凡是 cimicode 自己有能力承载的（系统指令、权限、模型配置、skills
发现），一律改用原生机制，自造辅助链路整体退役。

下面按"背景 → 问题 → 原则 → 框架 → 职责分工 → 差异明细"六层展开。

### 1.1 背景：旧形态（opencode）是如何工作的

worker-bridge 架构下，一个 worker = **bridge pod**（伪装成 worker 的 Matrix
身份，负责会话编排）+ **cimicode pod**（operator 供给的真实运行时，负责
对话与执行）。旧形态的完整工作链：

```
【供给链】Worker CR (runtime=worker-bridge)
  → operator watch → Deployment <w>-cimicode + svc（4096 runtime / 4097 helper）
    + Secret <w>-cimicode-fs（FS 凭据 ×2）
    + patch Worker env 三键（ADAPTER / BASE_URL / HELPER_URL）
  → bridge 自愈轮询读到三键，接上 cimicode pod

【镜像内】node:22-slim + npm install opencode-ai@1.18.27
  + vendored mc + 全套 skills + taskflow/agentteams-sync 包装
  + opencode.json（智谱 provider + ZHIPU_API_KEY build-arg 烘焙）
  + tools/bash.ts（同名 custom tool 覆盖内置 bash → 转发回环 helper /exec）
  + sandbox_helper.py（:4097，/agents-md 写 AGENTS.md + /exec）
  + emptyDir 挂 /workspace
  + entrypoint：发布 skills 到 $WORKDIR/.opencode/skills/ + 种子
    ~/.config/opencode/ + 起 helper + exec opencode serve

【每 turn】bridge: build_agent_md（Coordination 块 + AGENTS.md + SOUL.md）
  → POST :4097/agents-md（helper 原子写 $WORKDIR/AGENTS.md）
  → 会话自愈（GET/POST /session）
  → POST /session/{id}/message（阻塞整 turn）
  → 轮询 GET .../message 直到 info.time.completed
```

### 1.2 旧形态的结构性问题（为什么必须改）

换运行时（opencode npm 包 → cimicode 编译二进制）不是简单的替换 FROM，
旧形态里有五个**结构性**问题会被这次替换放大或直接撞上：

| # | 问题 | 具体表现 | 本次对策 |
|---|---|---|---|
| P-1 | **凭据烘焙进镜像** | `ZHIPU_API_KEY` 经 build-arg 写死在 `opencode.json`，模型（glm-5.3-flash 直连智谱）与平台解耦——换模型 = 重新构建镜像；key 出现在构建命令行/镜像层 | 镜像零凭据零模型；operator 供给期从 Worker CR 动态注入（§2.2） |
| P-2 | **系统指令靠自造文件链** | bridge→helper→写 AGENTS.md→opencode 从 cwd 发现，四跳链路；helper 挂了 turn 直接 RUNTIME_ERROR；且"AGENTS.md 变更仅新建会话生效"是旧版 opencode 的历史限制 | 切换到 cimicode 原生 `system` 消息字段，一跳、当 turn 实时、fail-loud 原子（§2.1） |
| P-3 | **custom tool 在编译二进制下不可加载** | `tools/bash.ts` 的 `import "@opencode-ai/plugin"` 要从配置目录 node_modules 运行时解析，cimicode 会后台 `npm install` 它——内网不可达必失败 | bash.ts 退役，用内置 bash（单容器同 cwd，转发本就无增益）（§2.5） |
| P-4 | **headless 权限盲区** | cimicode 首启写入企业默认权限 `{edit: ask, bash: ask}`，且任何未匹配权限默认 action 就是 `ask`——无客户端应答 = 无超时死等，每个碰工具的 turn 都会挂到超时 | `OPENCODE_PERMISSION={"*":"allow"}` env，结构上免疫首启 patcher（§2.3） |
| P-5 | **路径与惯例全是 opencode 旧约定** | `~/.config/opencode/`、`opencode.json`、`.opencode/`、`opencode serve`——cimicode 全部换了（`~/.cimi/cimicode/`、`cimicode.json`、`.cimicode/`、`cimicode serve`） | 路径以 cimicode 为准，逐项核源码后改写（§2.6） |

另有部署决策项：**去 emptyDir**（`/workspace` 改容器可写层，会话丢失由
bridge 404 自愈 + 每 turn system + taskflow mc pull 兜底，§2.7）。

### 1.3 核心设计原则（本次所有改动都由这五条推出）

1. **原生通道优先**：cimicode 自己有能力承载的，绝不再自造——系统指令走
   消息 `system` 字段（原生）、权限走 `OPENCODE_PERMISSION` env（原生）、
   模型配置走 `OPENCODE_CONFIG_CONTENT` env（原生合并优先级最高的配置源）、
   skills 走 `skills.paths` 配置键（原生发现）。自造链路（helper、bash.ts、
   烘焙 provider、拷贝发布 skills）只在 opencode 缺原生能力时才合理，cimicode
   时代全部退役。
2. **镜像零凭据零模型**：镜像只含静态能力（cimicode 二进制 + 协作工具 +
   skills + 权限默认）；一切环境相关的东西（模型、网关地址、key）由
   operator 供给期注入。换模型 = 改 Worker CR，不再是重构建镜像。
3. **声明式供给 + fail-loud**：operator 只做 level-triggered 收敛（幂等
   ensure/drift），模型变更经 env 哈希走 pod template 漂移滚动重启，无热改
   脚本；要素不齐（缺模型/缺网关 env/缺镜像名）就**推迟供给并大声报错**，
   绝不建一个跑不起来或行为残缺的 pod。
4. **路径与惯例以 cimicode 为准**：不迁就 opencode 旧约定（见 P-5 对照表，
   §2.6）；cimicode 自己的部署惯例（种 `~/.cimi/cimicode/`、coder 部署形态）
   就是我们的参照系。
5. **修改面收敛**：controller 零改动；`operator/` 原目录保留不动，cimicode
   相关修改全部落在**复制品** `operator-cimicode/`；bridge 只动 pod adapter
   传输层（system 字段 + 删 helper 接线），turn 编排/agent.md 渲染/协作协议
   一概不碰。这样每个组件的"为什么改"都能定位到唯一原则。

### 1.4 新形态整体框架：三大平面 + 四条数据流

```
╔══════════════════════════════════════════════════════════════════╗
║ 供给面 —— operator-cimicode（watch Worker CR，level-triggered）    ║
║   供给：Deployment <w>-cimicode（单端口）+ svc <w>-cimicode-svc    ║
║         + Secret <w>-cimicode-fs（accessKey/secretKey/model-config）║
║   patch：Worker env 两键 → bridge 自愈轮询接线                      ║
║   渲染：spec.model + bridge pod env → OPENCODE_CONFIG_CONTENT JSON ║
║   滚动：CIMICODE_MODEL_CONFIG_HASH 明文 env（内容变→模板漂移→重启）  ║
╚═══════════════════════════╤══════════════════════════════════════╝
                            │ 供给的 env / Secret / svc DNS
╔═══════════════════════════╧══════════════════════════════════════╗
║ 对话面 —— bridge pod（伪装 worker，controller 供给）                ║
║   Matrix 收消息 → turn 编排 → build_agent_md                       ║
║   CimicodePodAdapter.chat：                                       ║
║     ① 会话自愈（GET 探活 / 404 → POST /session 重建）              ║
║     ② baseline（最后一条 assistant id）                            ║
║     ③ POST /session/{id}/message                                  ║
║        body = {"system": <agent_md>, "parts": [...]}   ← agent.md 流 ║
║     ④ 轮询 GET .../message 直到 info.time.completed               ║
╚═══════════════════════════╤══════════════════════════════════════╝
                            │ http://<w>-cimicode-svc:4096（唯一端口）
╔═══════════════════════════╧══════════════════════════════════════╗
║ 运行面 —— cimicode pod（单容器，无 helper 无卷）                    ║
║   tini → entrypoint：种子 ~/.cimi/cimicode/cimicode.json（不覆盖） ║
║                      + 空 node_modules（跳过内网必败的 npm install）║
║         → exec cimicode serve :4096                               ║
║   消费：system 字段拼进 system prompt（agent.prompt+指令文件+system）║
║   权限：OPENCODE_PERMISSION（镜像 ENV，全放行）                     ║
║   模型：OPENCODE_CONFIG_CONTENT（secretKeyRef，优先级最高的配置源）  ║
║   skills：skills.paths 直读 /opt/agentteams/skills（零拷贝）        ║
║   执行：内置 bash 同容器运行 taskflow / agentteams-sync / mc        ║
║   工作区：/workspace = 容器可写层（无 emptyDir）                    ║
╚══════════════════════════════════════════════════════════════════╝
```

**四条数据流**（每条从源头到消费点，理解框架的钥匙）：

1. **agent.md 流（系统指令）**：源模板 AGENTS.md + runtime.yaml/SOUL/PROFILE
   （MinIO）→ bridge 每 turn `build_agent_md` 拼装（Coordination 块 + AGENTS.md
   + SOUL.md）→ **随消息 POST body 的 `system` 字段下发** → cimicode 持久化在
   User 消息上 → LLM 调用时拼进 system prompt。**当 turn 实时生效、历史不重复
   注入、与消息同体（原子）**。全程无文件落盘、无 helper。
2. **模型配置流**：Worker CR `spec.model` + bridge pod env
   （`AGENTTEAMS_AI_GATEWAY_URL` / `AGENTTEAMS_WORKER_GATEWAY_KEY`，controller
   组装的 Higress 网关三要素）→ operator `render_model_config` 渲染 cimicode
   配置方言（`agentteams-gateway` provider，OpenAI-compatible → 网关 `/v1`）
   → 存 Secret key `model-config` → Deployment 以 `OPENCODE_CONFIG_CONTENT`
   secretKeyRef 注入 → cimicode 合并时优先级最高，覆盖一切其他配置源。
   **与原生 worker/leader 链路同源**（openclaw 配置/runtime.yaml desired.model
   同一三要素）。变更生效 = 哈希 env 变 → pod 滚动重启（Secret 值变更本身
   不重启 pod，这是 K8s 语义，哈希是补齐闭环的钥匙）。
3. **任务协作流**（不变）：agent 经内置 bash 执行 `taskflow ack/submit`
   （任务协议状态机 + mc pull/push 任务目录）、`agentteams-sync`（shared/
   文件）、`mc`（MinIO 客户端）——依赖 operator 注入的 `AGENTTEAMS_*` 协作
   env（FS_ROOT/WORKER_NAME/FS_*/TEAM/MATRIX_USER_ID），产物落 MinIO
   `agents/<w>/` 与 `teams/{team}/shared/`。
4. **凭据流**：controller 组装 bridge pod 明文 env → operator 明文只读复制
   → Secret（FS accessKey/secretKey + model-config）→ cimicode pod 经
   secretKeyRef 注入。**Deployment spec 上不出现任何明文凭据**。

### 1.5 组件职责与修改面总表

| 组件 | 职责 | 本次修改 | 对应原则 |
|---|---|---|---|
| `cimicode-runtime/`（镜像） | 静态能力载体：cimicode 二进制 + 协作工具 + skills + 权限默认 | FROM cimicode 基础镜像；删 helper/bash.ts/烘焙 provider；`OPENCODE_PERMISSION` ENV；种子配置 skills.paths；薄 entrypoint | P1 P2 P4 |
| `operator-cimicode/`（供给器） | watch + 收敛供给 + 模型渲染 + 接线 patch + GC | 从 operator 复制后：模型注入、Secret 扩容、去 helper 端口、去 emptyDir、两键 patch | P2 P3 P5 |
| `bridge/`（对话编排） | Matrix 身份 + turn 编排 + agent.md 渲染 + runtime SPI | 仅 pod adapter 传输层：system 字段、删 helper 推送与接线；编排/渲染/协议不动 | P1 P5 |
| `operator/` + `opencode-runtime/`（备份） | 历史形态存档 | **零改动**，保留可回溯 | P5 |
| controller / 模板 / CLI | 协作协议所有方 | **零改动**（协作流原样） | P5 |
| Makefile / 契约 / README / changelog | 构建入口与文档权威 | 构建参数换血；契约升 v1.1 | — |

### 1.6 整体差异对比

| 维度 | 旧（opencode 形态） | 新（cimicode 形态） |
|---|---|---|
| 基础镜像 | `node:22-slim` + `npm install opencode-ai@1.18.27` | `FROM <internal-registry>/library/coder-cimicode:0.5.0`（ARG `CIMICODE_BASE_IMAGE` 可覆盖；实际 registry 地址见仓库内 Dockerfile/Makefile） |
| agent.md 通道 | bridge → helper `POST :4097/agents-md` → 原子写 `$WORKDIR/AGENTS.md` → opencode 从 cwd 发现 | bridge → **消息体 `system` 字段**（`POST /session/{id}/message` body），cimicode 原生消费，**当 turn 实时生效** |
| pod 内 helper | :4097 sandbox_helper.py（/agents-md + /exec + /healthz） | **整体删除**，单端口 :4096 |
| bash 工具 | `tools/bash.ts` 同名 custom tool 覆盖内置 bash，转发回环 helper | **删除**，直接用内置 bash（单容器同 cwd，转发无增益；custom tool 需运行时 npm install `@opencode-ai/plugin`，内网不可达） |
| LLM | `ZHIPU_API_KEY` build-arg 烘焙进 `opencode.json`（glm-5.3-flash 直连智谱） | **镜像零凭据零模型**：operator 从 Worker CR `spec.model` + bridge pod env 渲染 cimicode 配置方言，经 `OPENCODE_CONFIG_CONTENT` env（secretKeyRef）注入，与原生 worker/leader 链路（Higress AI 网关）同源 |
| 权限 | 无显式处理 | `ENV OPENCODE_PERMISSION={"*":"allow"}`（headless 防 ask 死锁，见 §2.3） |
| skills 发布 | entrypoint 每次启动拷贝到 `$WORKDIR/.opencode/skills/` | 种子配置 `skills.paths` **直读** `/opt/agentteams/skills`，零拷贝 |
| 配置路径 | `~/.config/opencode/opencode.json` | `~/.cimi/cimicode/cimicode.json`（存在即不覆盖）+ 预建空 `node_modules` 跳过后台 npm install |
| 工作卷 | emptyDir 挂 `/workspace` | **无卷**，`/workspace` = 容器可写层（404 自愈 + 每 turn system + mc pull 兜底） |
| operator | `worker-bridge/operator`（双端口 svc、三键 patch、FS Secret 2 key） | **复制为 `worker-bridge/operator-cimicode`**：单端口 svc、两键 patch、Secret 3 key（+model-config）、模型注入、无卷。**原目录不动** |
| 端口契约 | 4096 runtime + 4097 helper | **仅 4096** |
| Worker env 接线 | `BRIDGE_RUNTIME_ADAPTER` / `_BASE_URL` / `_HELPER_URL` 三键 | 两键（`_HELPER_URL` 删除） |

### 1.7 不变的部分（以及为什么可以不变）

- **协作协议层全部不变**：源模板 `template/worker-bridge-agent/`（AGENTS.md
  骨架 + 5 skills）、taskflow / agentteams-sync / mc、统一 JSONL 日志——它们
  只依赖 bash + MinIO env 契约，与运行时无耦合（原则 P5 的收益：换运行时
  不触碰协议）。
- **bridge 编排层不变**：turn 编排、`build_agent_md` 拼装、runtime.yaml 投影、
  stateless 形态、自愈轮询——pod adapter 对它们只是一个传输插件。
- **controller 零改动**：bridge pod env（网关 key/URL/FS 凭据）、runtime.yaml
  投影、TeamReconciler 全部原样，operator 从同一源头取数。
- **REST 轮询形态不变**：SSE 在近期版本仍不可靠，轮询是标定结论，勿回退。
- **协作 env 契约不变**：`AGENTTEAMS_FS_ROOT / WORKER_NAME / FS_* / TEAM /
  MATRIX_USER_ID`，且**仍不得设置 `AGENTTEAMS_RUNTIME`**（mc 同步只认
  k8s/aliyun，本 pod 走 local 静态三元组）。

---

## 2. 设计决策详解（每条附 cimicode 源码依据）

### 2.1 agent.md 改走消息体 `system` 字段（本次核心变更）

**通道对比（三选一，选 B）**：

- **A（旧）**：bridge → helper HTTP → 写 `$WORKDIR/AGENTS.md` → cimicode 从 cwd
  向上发现。cimicode 的 `instruction.system()` 在 agent 循环**每个 step 重建且无
  缓存**（`session/prompt.ts:1444`，`step===1`/`step>1` 循环体内），其实也每 turn
  实时；但链条长（bridge→helper→文件→发现），helper 挂了 turn 直接 RUNTIME_ERROR。
- **B（选定）**：`POST /session/{id}/message` 的 body 带顶层 `"system"` 字段
  （`session/prompt.ts:1701` `PromptInput.system`）。
- **C（否决）**：种子配置 `instructions: ["http://.../agent-md"]`，cimicode 每
  step 重新 fetch（`session/instruction.ts:143-171` 支持 http(s) URL）。否决原因：
  **fetch 失败静默降级**——5s 超时 + catch 返回空串（`instruction.ts:105-113`），
  agent 会在丢失全部协作协议的情况下继续跑 turn 而无人知晓。

**B 的生效机制（逐行核验）**：
1. `system` 随消息持久化到 User 消息（`prompt.ts:922` → `message-v2.ts:398`）；
2. LLM 调用时拼 system prompt：`agent.prompt + 指令文件 + input.user.system`
   三段拼接，我们的 agent.md 排最后（`session/llm.ts:100-112`）；
3. **历史消息转换只转 parts、不含 `system` 字段**
   （`message-v2.ts:791-837` `toModelMessagesEffect`）——只在当前 turn 生效，
   历史不重复注入、无 token 膨胀。

**性质**：实时（turn N+1 的团队名单变化当 turn 即生效）、fail-loud 且原子
（agent.md 与消息同体提交，无中间态）、pod 内不再需要任何文件写入 →
helper / 4097 / `BRIDGE_RUNTIME_HELPER_URL` 全链路退役。

### 2.2 模型注入：与原生 worker/leader 链路同源

原生链路（AgentTeams 主仓库）：
- openclaw worker/leader：controller `agentconfig/generator.go:125-143` 生成
  `models.providers.agentteams-gateway = {baseUrl: <aiGatewayURL>/v1, apiKey:
  <GatewayKey>, api: openai-completions}`，`model.primary = agentteams-gateway/<model>`；
- copaw/qwenpaw worker：controller 写 MinIO `agents/<w>/runtime/runtime.yaml`
  `desired.model: {providerId: agentteams-gateway, model, gatewayUrl, gatewayKey}`
  （`internal/service/runtime_config.go:303-312`）；
- 数据源头：Worker CR `spec.model` + `spec.modelProvider`；网关三要素经
  `internal/service/worker_env.go:30/114` 注入 **bridge pod env**：
  `AGENTTEAMS_WORKER_GATEWAY_KEY`（Higress consumer key）、
  `AGENTTEAMS_AI_GATEWAY_URL`（modelProvider 的 IntranetURL 或集群默认）。

operator-cimicode 照抄这条路：它本来就从 bridge pod env 明文只读复制 FS 凭据，
同一来源顺路取模型三要素，渲染成 **cimicode 配置方言**：

```json
{"model": "agentteams-gateway/<model>",
 "provider": {"agentteams-gateway": {
   "npm": "@ai-sdk/openai-compatible",
   "options": {"baseURL": "<gw>/v1", "apiKey": "<key>"},
   "models": {"<model>": {"name": "<model>"}}}}}
```

注入机制 = `OPENCODE_CONFIG_CONTENT` env：cimicode 在所有配置源（全局默认、
OPENCODE_CONFIG 文件、项目 cimicode.json、配置目录）**之后**合并它
（`config/config.ts:618-626`，source="local"），优先级最高。

**凭据与滚动重启**：JSON 含 gateway key，整体放 Secret（key 名 `model-config`），
Deployment spec 只见 secretKeyRef——延续 operator 既有"凭据不经明文 env"原则。
K8s 的 Secret 值变更不重启 pod，因此配一个**明文哈希 env**
`CIMICODE_MODEL_CONFIG_HASH = sha256(content)[:16]`：模型/网关/key 任一变化 →
哈希变 → pod template env 指纹漂移 → 滚动重启生效（声明式，无热改脚本）。

**边界语义**：
- `spec.model == "native-config"`（哨兵，与 controller `isNativeConfigModel`
  一致）→ 跳过注入，镜像默认配置生效；
- `spec.model` 为空 / bridge pod env 缺网关要素 → **fail-loud 推迟供给**
  （同 `CIMICODE_IMAGE` 缺失的模式），不建一个跑不起来 turn 的 pod。

### 2.3 权限：`OPENCODE_PERMISSION={"*":"allow"}`（headless 防死锁）

三个源码事实：
1. 权限评估到 `ask` 时发布事件后 **await 一个无超时的 Deferred**
   （`permission/index.ts:206-214`），只有客户端 reply 才解锁——bridge 是唯一
   客户端且不应答权限弹窗 → 每次 bash/edit 调用挂到 bridge turn 超时；
2. **任何没有匹配规则的权限，默认 action 就是 `ask`**
   （`permission/evaluate.ts:14`）——不止 read/edit/bash，webfetch/skill/mcp
   等全算，必须全覆盖；
3. cimicode 首启会把企业默认权限
   `{read: allow, edit: ask, bash: ask}` 写进全局配置；对已存在文件只补缺失
   key，**但对字符串简写形式是整体替换**（`config/config.ts:500-502`）——
   所以不能在配置文件里写 `permission: "allow"`（会被当场销毁）。

**解法**：`Flag.OPENCODE_PERMISSION` env（JSON 字符串）在全局配置加载**之后**
merge（`config/config.ts:696-698`），结构上免疫首启 patcher。镜像 Dockerfile 里
`ENV OPENCODE_PERMISSION={"*":"allow"}` 一条搞定全部权限面；operator 将来要按
team 定策略时在 Deployment env 同名覆盖即可（container env 优先于 image ENV）。
种子配置文件里**不写 permission**（避免两处权威）。

### 2.4 skills：`skills.paths` 直读镜像目录（零拷贝）

cimicode 的 skills 发现顺序（`skill/index.ts:182-240`）：内置
`~/.cimi/cimicode/skills`（最低优先级）→ `.claude`/`.agents` 外部目录 → 各配置
目录 `{skill,skills}/**/SKILL.md` → **配置键 `skills.paths`（任意绝对路径，
`skill/index.ts:218`）** → `skills.urls`。

选 `skills.paths: ["/opt/agentteams/skills"]` 写进种子配置：只读、镜像内副本权威、
不往工作目录拷任何东西（工作目录里唯一动态内容就是任务数据本身）。旧做法（拷到
工作目录 `.opencode/skills/`）会让 skills 混进 mc 同步地盘、可能被 agent 误删。

附带事实：cimicode 的内置 skills 同步只在**二进制旁有 `skills/` 目录**时才发生
（`skill/index.ts:52-53`），0.5.0 镜像 `/usr/local/bin/cimicode` 是单文件 → 不会
往 agent 的 skill 列表塞无关技能。skill 工具 shell 出 `rg` 扫描，base 镜像已带
`/usr/local/bin/rg`。

### 2.5 bash.ts 与 pod 内 helper 退役

- custom tool 加载机制仍在（`tool/registry.ts:163-176` 扫描配置目录
  `{tool,tools}/*.{js,ts}`，文件名即工具 id；同 id 时 custom 后写覆盖 builtin，
  `session/prompt.ts:413` 的 map 覆写顺序）。**但** `tools/*.ts` 是运行时被编译
  二进制动态 import 的，裸包名 `@opencode-ai/plugin` 要从该配置目录的
  node_modules 解析——cimicode 会后台 `npm install` 它（`config/config.ts:586-594`），
  **内网不可达必失败**，custom tool 加载不出来。
- 单容器形态下内置 bash 本就在本容器、同 cwd 执行，bash.ts 转发回环 helper 的
  增益≈0。结论：bash.ts 删除、helper 删除、`/exec` 不留（kubectl exec 进 pod
  就有 shell）。
- **连带 trick**：预建空的 `$HOME/.cimi/cimicode/node_modules/` 目录——
  `core/npm.ts:143` 的 install 只查 `node_modules` 目录存在性，空目录让它直接
  跳过那次注定失败的 npm install 尝试（连 warn 日志都没有）。

### 2.6 路径全部以 cimicode 为准

| 项 | opencode | cimicode | 源码依据 |
|---|---|---|---|
| 全局配置目录 | `~/.config/opencode` | `~/.cimi/cimicode` | `packages/core/src/global.ts:12` |
| 全局/项目配置文件名 | `opencode.json` | `cimicode.json` | `config/config.ts:555/574` |
| 项目配置目录 | `.opencode` | `.cimicode` | `config/paths.ts:31` |
| serve 命令 | `opencode serve` | `cimicode serve --port N --hostname 0.0.0.0` | `cli/cmd/serve.ts`；无 `OPENCODE_SERVER_PASSWORD` 仅告警不阻断 |
| AGENTS.md 发现 | cwd 向上 | **同名不变**（`session/instruction.ts:17`，FILES 首位） | 本方案已不再依赖（走 system 字段） |

种子时机在 entrypoint（存在即不覆盖，保留排障时挂 ConfigMap 手改的口子）。
**不用 `OPENCODE_CONFIG_DIR`**——它是追加型（`config/paths.ts:25-43` 返回列表），
与默认目录合并而非独占，既然要种默认目录就没必要多一个概念。

### 2.7 去 emptyDir

`/workspace` 直接落容器可写层。代价：容器重启也丢会话——由既有兜底覆盖
（bridge 404 自愈重建会话 + 每 turn 重发 system + taskflow `mc pull` 拉任务
目录），只是触发更频繁。PVC 化仍是后续生产化步骤。

### 2.8 REST 契约兼容性（已核验，需冒烟）

`/session`、`/session/{id}`、`/session/{id}/message` 路由全在
（`server/routes/instance/httpapi/groups/session.ts:73-88`）；Session.Info 顶层
`id`；assistant 消息仍有 `info.time.completed` / `info.error`
（`session/message-v2.ts:549-553`）；`{"system":..., "parts":[{"type":"text",...}]}`
payload 兼容。**注意**：这是仓库 HEAD 的结论，0.5.0 镜像内 cimicode 的版本对齐
需冒烟确认（记入契约"已知限制"）。

---

## 3. 改动文件清单

| 文件 | 动作 | 说明 |
|---|---|---|
| `worker-bridge/opencode-runtime/` | **新建（备份）** | 原 `cimicode-runtime/` 逐字节拷贝，保留不动、不再构建 |
| `worker-bridge/cimicode-runtime/Dockerfile` | 重写 | §6.1 全文 |
| `worker-bridge/cimicode-runtime/cimicode.json` | 新建 | §6.2 全文 |
| `worker-bridge/cimicode-runtime/entrypoint.sh` | 重写 | §6.3 全文 |
| `worker-bridge/cimicode-runtime/opencode.json` | **删除** | 智谱 provider 烘焙退役（备份里有） |
| `worker-bridge/cimicode-runtime/sandbox_helper.py` | **删除** | helper 整体退役（备份里有） |
| `worker-bridge/cimicode-runtime/tools/bash.ts` | **删除** | custom bash 转发退役（备份里有） |
| `worker-bridge/operator-cimicode/` | **新建** | 原 `operator/` 整目录拷贝后改 `worker_bridge_operator.py`（§6.5 六处 hunk）+ `deploy/operator.yaml`（§6.6）。**`operator/` 原目录不动**；`Dockerfile` 拷贝后无需改 |
| `worker-bridge/bridge/src/cimicode_bridge/runtime/cimicode_pod_adapter.py` | 修改 | §6.7 三处 hunk |
| `worker-bridge/bridge/src/cimicode_bridge/runtime/registry.py` | 修改 | §6.8 全文 |
| `worker-bridge/bridge/src/cimicode_bridge/config.py` | 修改 | §6.9 一行删除 |
| `worker-bridge/bridge/src/cimicode_bridge/app.py` | 修改 | §6.10 三处 hunk |
| `worker-bridge/bridge/src/cimicode_bridge/bootstrap.py` | 修改 | §6.11 一段删除 |
| `worker-bridge/contract/adapter-contract.md` | 重写 | §6.12 全文（v1.1） |
| `Makefile` | 修改 | §6.13 两处 hunk |
| `worker-bridge/README.md` | 修改 | §6.14（文档性，概述即可） |
| `changelog/current.md` | 追加 | §6.15 |
| 测试（本文不含代码） | 修改 | `bridge/tests/unit/` 三个文件 + `operator-cimicode/tests/`：适配 system 字段/两键 patch/无卷/模型注入，删 helper 用例（按上述源码改动自行对应适配） |

**重放顺序建议**：① 备份两个目录 → ② cimicode-runtime 四件 → ③ operator-cimicode
→ ④ bridge 五件 → ⑤ Makefile → ⑥ 契约/README/changelog（测试按源码改动自行对应适配）。

---

## 4. 运行时形态总图（新）

```
                    Matrix 房间
                        │
        ┌───────────────┴───────────────┐
        │  bridge pod（伪装 worker，controller 供给）
        │  每 turn: build_agent_md（Coordination+AGENTS.md+SOUL.md）
        │  CimicodePodAdapter.chat:
        │    1) 会话自愈 GET/POST /session（404 重建）
        │    2) baseline 最后一条 assistant id
        │    3) POST /session/{id}/message
        │       body = {"system": <agent_md>, "parts":[{type:text,text:...}]}
        │    4) 轮询 GET .../message 直到 info.time.completed
        └───────────────┬───────────────┘
                        │ http://<w>-cimicode-svc:4096（operator-cimicode 供给）
        ┌───────────────┴───────────────────────────────────────┐
        │  cimicode pod（Deployment <w>-cimicode，单容器单端口）  │
        │  tini → entrypoint.sh:                                  │
        │    种子 ~/.cimi/cimicode/cimicode.json（存在不覆盖）      │
        │    mkdir 空 ~/.cimi/cimicode/node_modules                │
        │    exec cimicode serve :4096                             │
        │  env: AGENTTEAMS_*（协作） + OPENCODE_PERMISSION（镜像）  │
        │       + OPENCODE_CONFIG_CONTENT（secretKeyRef，模型）     │
        │  /workspace = 容器可写层（无卷）                          │
        │  内置 bash 同容器执行 taskflow/agentteams-sync/mc         │
        └─────────────────────────────────────────────────────────┘
   Secret <w>-cimicode-fs: accessKey / secretKey / model-config
   （operator-cimicode 从 bridge pod env 明文只读复制渲染）
```

---

## 5. 新的 env 契约（operator-cimicode → cimicode pod）

| env | 来源 | 说明 |
|---|---|---|
| `AGENTTEAMS_FS_ROOT` | operator 明文 | `/workspace` |
| `AGENTTEAMS_WORKER_NAME` | operator 明文 | `agents/<w>/` 前缀解析 |
| `AGENTTEAMS_FS_ENDPOINT` / `_BUCKET` | operator 明文 | MinIO 端点/桶 |
| `AGENTTEAMS_FS_ACCESS_KEY` / `_SECRET_KEY` | **secretKeyRef** | 存储 凭据 |
| `AGENTTEAMS_TEAM` | operator 明文 | Team CR 名（有才设） |
| `AGENTTEAMS_MATRIX_USER_ID` | operator 明文 | taskflow `--actor` |
| `OPENCODE_CONFIG_CONTENT` | **secretKeyRef**（key `model-config`） | 模型/provider 配置（cimicode 合并优先级最高） |
| `CIMICODE_MODEL_CONFIG_HASH` | operator 明文 | sha256(model-config)[:16]，内容变更 → 滚动重启 |
| `OPENCODE_PERMISSION` | **镜像 ENV** | `{"*":"allow"}`，operator 可同名覆盖 |
| `OPENCODE_PORT` | 镜像 ENV | `4096` |
| ❌ `AGENTTEAMS_RUNTIME` | **绝不设置** | mc 同步会误走 k8s 模式 |

Worker CR `spec.env` 被 operator patch 两键（bridge 消费）：
`BRIDGE_RUNTIME_ADAPTER=cimicode-pod`、
`BRIDGE_RUNTIME_BASE_URL=http://<w>-cimicode-svc.<ns>.svc:4096`。

---

## 6. 全部代码改动

### 6.1 `worker-bridge/cimicode-runtime/Dockerfile`（重写，全文）

```dockerfile
# =============================================================================
# cimicode-runtime — cimicode pod 合并镜像（cimicode + 任务协作工具 + skills）
# =============================================================================
# 基于内部 coder-cimicode 基础镜像（cimicode = 换皮 opencode，bun 编译单二进
# 制），cimicode-pod 模式的运行时 pod 单容器承载对话与执行（无独立 sandbox
# pod，也无 pod 内 helper）：
#   1. cimicode serve（:4096，bridge 的 turn 入口，REST 契约见
#      worker-bridge/contract/adapter-contract.md）
#   2. 全套 worker skills + taskflow / agentteams-sync / mc / jq / git
#   3. LLM：operator-cimicode 供给时从 Worker CR spec.model + bridge pod env
#      （AGENTTEAMS_AI_GATEWAY_URL / AGENTTEAMS_WORKER_GATEWAY_KEY）渲染
#      OPENCODE_CONFIG_CONTENT 注入——镜像不烘焙任何模型与凭据
#
# agent.md 通道：消息体 system 字段（cimicode 原生）——bridge 每 turn 重拼
# 的 agent.md 随 POST /session/{id}/message 下发，当前 turn 即生效（历史消
# 息转换不含该字段，无重复注入）。旧链路（pod 内 helper POST /agents-md 落
# cwd AGENTS.md）已整体退役，本镜像只开 :4096 一个端口。
#
# 与旧 opencode 形态（原始备份见 worker-bridge/opencode-runtime/）的关键差异：
#   - bash.ts 转发工具与 pod 内 helper 退役：单容器形态下内置 bash 本就在
#     本容器、同 cwd 执行；custom tool 需要运行时往配置目录 npm install
#     @opencode-ai/plugin（内网不可达），一并去掉
#   - 权限走 ENV OPENCODE_PERMISSION={"*":"allow"}：cimicode 在全局配置加载
#     后 merge 此 env，免疫首启默认权限 patcher（headless 无人应答权限弹窗，
#     ask = 无超时死等——未匹配权限的默认 action 也是 ask）
#   - 路径以 cimicode 为准：全局配置 $HOME/.cimi/cimicode/cimicode.json，
#     skills 经种子配置 skills.paths 直读 /opt/agentteams/skills（零拷贝，
#     不往工作目录发布任何东西）
#
# 构建上下文 = 仓库根（见 Makefile build-cimicode-runtime）：
#   docker build -f worker-bridge/cimicode-runtime/Dockerfile \
#     --build-arg CIMICODE_BASE_IMAGE=<internal-registry>/... \
#     -t agentteams/cimicode-runtime:<tag> .
#
# 镜像内不含凭据、模型与节点名。
#
# 运行时 env 契约（operator-cimicode 供给 Deployment 时注入；FS_* 与模型配
# 置走 Secret）：
#   AGENTTEAMS_FS_ROOT           工作目录（默认 /workspace，容器可写层）
#   AGENTTEAMS_WORKER_NAME       worker 名（agents/<w>/ 前缀解析）
#   AGENTTEAMS_FS_ENDPOINT / AGENTTEAMS_FS_BUCKET        MinIO 端点/桶（明文）
#   AGENTTEAMS_FS_ACCESS_KEY / AGENTTEAMS_FS_SECRET_KEY  存储凭据（secretKeyRef）
#   AGENTTEAMS_TEAM              Team CR 名（有才设）
#   AGENTTEAMS_MATRIX_USER_ID    worker 的 Matrix MXID（taskflow --actor）
#   OPENCODE_CONFIG_CONTENT      模型/provider 配置（secretKeyRef，operator 渲染）
#   注意：不得设置 AGENTTEAMS_RUNTIME（mc 同步会误走 k8s 模式，local 三元组
#   才是本 pod 的契约——见 worker-bridge/cimicode-sandbox 同款说明）
# =============================================================================
ARG CIMICODE_BASE_IMAGE=<internal-registry>/library/coder-cimicode:0.5.0
FROM ${CIMICODE_BASE_IMAGE}

# base 镜像缺的协作/运行依赖（其 APT 源已指向内网镜像源）：
#   git / jq / procps / curl  协作工具链与排障依赖
#   tini                      进程收尸与信号转发
#   python3（taskflow 等 CLI）、ripgrep（skill 工具 shell 出 rg）、Node.js、
#   busybox  base 已带，不再重装。
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl git jq procps tini \
    && rm -rf /var/lib/apt/lists/*

# mc（MinIO 客户端，taskflow / agentteams-sync 的同步通道）：vendored 二进制
# 直供，不走构建期下载——dl.min.io 的 latest 式路径已下线（410），且真实
# 内网构建无外网。版本 RELEASE.2025-08-13T08-35-41Z（linux-amd64）。
COPY worker-bridge/cimicode-runtime/bin/mc /usr/local/bin/mc
RUN chmod +x /usr/local/bin/mc

# ── 1. 种子配置 + entrypoint ─────────────────────────────────────────────
COPY worker-bridge/cimicode-runtime/cimicode.json /opt/agentteams/cimicode.json
COPY worker-bridge/cimicode-runtime/entrypoint.sh /opt/agentteams/entrypoint.sh
# sed 剥 CR：CRLF shebang（#!/bin/sh\r）会让 exec 误报 no such file or
# directory——Windows 编辑过的文件不得打断启动。
RUN sed -i "s|\r$||" /opt/agentteams/entrypoint.sh \
    && chmod +x /opt/agentteams/entrypoint.sh

# ── 2. skills 全套 + 协作协议 CLI（全局命令包装到 PATH）──────────────────
COPY worker-bridge/template/worker-bridge-agent/skills /opt/agentteams/skills
RUN printf '#!/bin/sh\nexec python3 /opt/agentteams/skills/task-management/scripts/taskflow.py "$@"\n' > /usr/local/bin/taskflow \
    && printf '#!/bin/sh\nexec python3 /opt/agentteams/skills/file-sharing/scripts/agentteams_sync.py "$@"\n' > /usr/local/bin/agentteams-sync \
    && chmod +x /usr/local/bin/taskflow /usr/local/bin/agentteams-sync \
    && chmod +x /opt/agentteams/skills/*/scripts/*.py || true

# ── 3. 约定 ──────────────────────────────────────────────────────────────
# OPENCODE_PERMISSION：headless 全放行。未匹配权限的默认 action 是 ask
# （permission/evaluate.ts），ask 在无客户端应答时是无超时死等；bridge 是
# 唯一客户端且不应答权限弹窗。此 env 在全局配置加载后 merge（含首启默认
# 权限 patcher 之后），是权限面的唯一权威——种子配置里不写 permission。
ENV AGENTTEAMS_SKILLS_ROOT=/opt/agentteams/skills \
    OPENCODE_PORT=4096 \
    OPENCODE_PERMISSION={"*":"allow"}
EXPOSE 4096
WORKDIR /workspace
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/agentteams/entrypoint.sh"]
```

**与旧版 Dockerfile 的实质差异**：`FROM node:22-slim` → `ARG CIMICODE_BASE_IMAGE`
+ `FROM ${CIMICODE_BASE_IMAGE}`；删 `ARG OPENCODE_VERSION` / `ARG NPM_REGISTRY` /
`ARG ZHIPU_API_KEY` 及整段 npm install opencode-ai + sed 替换 apiKey 校验；删
`COPY sandbox_helper.py` / `COPY opencode.json` / `COPY tools/bash.ts`；apt 列表
去 `ripgrep python3`（base 已带）保留 `ca-certificates tini curl jq git procps`；
`EXPOSE 4096 4097` → `EXPOSE 4096`；ENV 删 `BRIDGE_SANDBOX_HELPER_PORT`、增
`OPENCODE_PERMISSION`。mc vendored COPY、skills COPY、taskflow/agentteams-sync
包装、tini entrypoint、CRLF 剥除全部保持。

### 6.2 `worker-bridge/cimicode-runtime/cimicode.json`（新建，全文）

```json
{
  "$schema": "https://opencode.ai/config.json",
  "autoupdate": false,
  "skills": {
    "paths": ["/opt/agentteams/skills"]
  }
}
```

注意：**不写 `permission`**（权限权威在 `OPENCODE_PERMISSION` env，见 §2.3）；
**不写 provider/model**（operator 注入，见 §2.2）。

### 6.3 `worker-bridge/cimicode-runtime/entrypoint.sh`（重写，全文）

```sh
#!/bin/sh
# cimicode pod entrypoint（单容器，基于内部 cimicode 换皮 opencode）：
#   1. 种子 cimicode 全局配置到 $HOME/.cimi/cimicode/（存在即不覆盖——排障
#      时可挂 ConfigMap 手改）；skills 经种子配置的 skills.paths 直读镜像目
#      录 /opt/agentteams/skills，零拷贝、不往工作目录发布任何东西；
#   2. 预建空 node_modules——cimicode 对每个配置目录做 node_modules 存在性
#      检查（core/npm.ts），缺则后台 npm install @opencode-ai/plugin（内网
#      不可达，注定失败）——空目录让它直接跳过（custom tool 链路已退役，
#      本无 tools/*.ts 需要加载）；
#   3. 前台 exec cimicode serve（:4096，bridge 的 turn 入口）。
# agent.md 不经本脚本：bridge 把每 turn 重拼的 agent.md 放在消息体的
# system 字段（cimicode 原生通道）随 POST /session/{id}/message 下发，当
# turn 即生效——无 pod 内 helper、无 AGENTS.md 文件落盘。
# 工作目录无 emptyDir：/workspace 即容器可写层；pod 重建丢会话由 bridge 的
# 404 自愈 + taskflow mc pull 兜住。
# 模型配置也不经本脚本：operator-cimicode 以 OPENCODE_CONFIG_CONTENT env
# （secretKeyRef）注入，cimicode 原生消费（合并优先级最高）。
set -eu

WORKDIR="${AGENTTEAMS_FS_ROOT:-/workspace}"
OPENCODE_PORT="${OPENCODE_PORT:-4096}"

mkdir -p "$WORKDIR"
cd "$WORKDIR"

export AGENTTEAMS_FS_ROOT="$WORKDIR"

# 种子全局配置（cimicode 全局配置目录 = $HOME/.cimi/cimicode，配置文件名
# cimicode.json——见 agi-opencode packages/core/src/global.ts）
CFG_DIR="$HOME/.cimi/cimicode"
mkdir -p "$CFG_DIR"
if [ ! -f "$CFG_DIR/cimicode.json" ]; then
    cp /opt/agentteams/cimicode.json "$CFG_DIR/cimicode.json"
    echo "[cimicode] seeded global config to $CFG_DIR/cimicode.json"
fi
# 空 node_modules：跳过后台 npm install 尝试（见文件头注释）
mkdir -p "$CFG_DIR/node_modules"

echo "[cimicode] workdir=$WORKDIR port=$OPENCODE_PORT"
exec cimicode serve --port "$OPENCODE_PORT" --hostname 0.0.0.0
```

**与旧版 entrypoint 的实质差异**：删"发布 skills 到 `$WORKDIR/.opencode/skills/`"
整段（改 skills.paths 直读）；删"种子 opencode 全局配置到
`$HOME/.config/opencode` + 拷 bash.ts"段（改 `~/.cimi/cimicode` + 无 bash.ts）；
删 `export OPENCODE_WORKDIR` / `export SANDBOX_EXEC_URL`；删"后台起
sandbox_helper"段；尾行 `exec opencode serve` → `exec cimicode serve`；新增空
`node_modules` 预建。

### 6.4 cimicode-runtime 删除的三个文件

- `opencode.json`（旧智谱 provider 烘焙模板，含 `__ZHIPU_API_KEY__` 占位符）
- `sandbox_helper.py`（:4097 helper：/agents-md、/exec、/healthz）
- `tools/bash.ts`（同名 custom bash 转发工具）

三者均完整保留在 `worker-bridge/opencode-runtime/` 备份中。`bin/mc`（vendored
MinIO 客户端）保留不动。

### 6.5 `worker-bridge/operator-cimicode/worker_bridge_operator.py`

**先整目录拷贝 `worker-bridge/operator` → `worker-bridge/operator-cimicode`**
（`Dockerfile` 无需改动；它 COPY 的是目录内同名的 `worker_bridge_operator.py`），
然后对 `worker_bridge_operator.py` 做以下六处修改：

**hunk 1 — imports 增加两行**（在 `import base64` 之后）：

```python
import base64
import hashlib
import json
```

**hunk 2 — 常量区**：删除 `ENV_KEY_HELPER_URL = "BRIDGE_RUNTIME_HELPER_URL"`
一行；在 `SECRET_KEY_SECRET = "secretKey"` 之后追加：

```python
# 模型注入（与原生 worker/leader 链路同源：controller agentconfig/generator.go
# 的 agentteams-gateway provider = Higress AI 网关 OpenAI 兼容端点）：
#   model   = Worker CR spec.model（trim "agentteams-gateway/" 前缀；
#             "native-config" 哨兵 = 不注入，镜像默认配置生效）
#   baseURL = bridge pod env AGENTTEAMS_AI_GATEWAY_URL + "/v1"
#   apiKey  = bridge pod env AGENTTEAMS_WORKER_GATEWAY_KEY（Higress consumer key）
# 渲染成 cimicode 配置方言经 OPENCODE_CONFIG_CONTENT env 注入（cimicode 合并
# 优先级最高的配置源）；JSON 含 key，整体放 Secret，Deployment spec 只见
# secretKeyRef + 明文内容哈希 env——Secret 值变更不触发 pod 重启，哈希变更经
# pod template 漂移滚动生效。
AI_GATEWAY_URL_ENV = "AGENTTEAMS_AI_GATEWAY_URL"
GATEWAY_KEY_ENV = "AGENTTEAMS_WORKER_GATEWAY_KEY"
SECRET_KEY_MODEL_CONFIG = "model-config"
ENV_MODEL_CONFIG = "OPENCODE_CONFIG_CONTENT"
ENV_MODEL_CONFIG_HASH = "CIMICODE_MODEL_CONFIG_HASH"
PROVIDER_ID = "agentteams-gateway"
NATIVE_CONFIG_MODEL = "native-config"
```

**hunk 3 — `OperatorConfig.__init__`**：删除 `self.cimicode_helper_port =
int(env("CIMICODE_HELPER_PORT", "4097"))` 及其上方两行端口对注释，替换为：

```python
        # Port pinned by the cimicode-runtime image contract (cimicode serve;
        # agent.md rides the message body system field — no helper port).
        self.cimicode_port = int(env("CIMICODE_PORT", "4096"))
```

**hunk 4 — `fs_credentials` 方法之后新增两个方法**：

```python
    # ------------------------------------------------------------------
    # model config（cimicode 方言，OPENCODE_CONFIG_CONTENT 消费）
    # ------------------------------------------------------------------

    def model_config_content(
        self, worker_obj: dict[str, Any], bridge_env: dict[str, str]
    ) -> tuple[str | None, str]:
        """渲染 cimicode 模型/provider 配置 JSON。

        返回 (content, problem)：
          content 非 None        → 注入（problem 恒空）
          content None, problem  → 推迟供给（fail-loud：模型缺失或网关要素
                                   不在 bridge pod env——先别建一个跑不起来
                                   的 pod）
          content None, problem空 → 合法跳过（native-config 哨兵：镜像默认
                                   配置生效，与 controller 语义一致）
        """
        spec_model = str(worker_obj.get("spec", {}).get("model") or "").strip()
        if not spec_model:
            return None, "spec.model is empty"
        if spec_model.lower() == NATIVE_CONFIG_MODEL:
            return None, ""
        gateway_url = (bridge_env.get(AI_GATEWAY_URL_ENV) or "").strip().rstrip("/")
        gateway_key = (bridge_env.get(GATEWAY_KEY_ENV) or "").strip()
        if not gateway_url or not gateway_key:
            missing = AI_GATEWAY_URL_ENV if not gateway_url else GATEWAY_KEY_ENV
            return None, f"bridge pod env carries no {missing}"
        return self.render_model_config(spec_model, gateway_url, gateway_key), ""

    @staticmethod
    def render_model_config(model: str, gateway_url: str, gateway_key: str) -> str:
        """cimicode 配置方言：provider agentteams-gateway（openai-compatible）
        指向 Higress AI 网关 /v1，model 主键 agentteams-gateway/<model>——
        与 openclaw 配置/runtime.yaml desired.model 的原生链路同型。"""
        model = model.removeprefix(f"{PROVIDER_ID}/")
        base_url = gateway_url.rstrip("/")
        config = {
            "model": f"{PROVIDER_ID}/{model}",
            "provider": {
                PROVIDER_ID: {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "AgentTeams AI Gateway",
                    "options": {"baseURL": f"{base_url}/v1", "apiKey": gateway_key},
                    "models": {model: {"name": model}},
                },
            },
        }
        return json.dumps(config, ensure_ascii=False)
```

**hunk 5 — `cimicode_deployment` 方法整体替换**（签名增 `model_config` 参数；
删 `SANDBOX_EXEC_URL` env、helper 端口、`volume_mounts`、`volumes`、emptyDir
注释；FS secretKeyRef 的 `secret_name` 提前到公共作用域；新增模型 env 块）：

```python
    def cimicode_deployment(
        self,
        worker: str,
        *,
        team: str = "",
        matrix_user: str = "",
        with_fs_secret: bool = False,
        model_config: str | None = None,
    ) -> client.V1Deployment:
        name = self.cimicode_deploy_name(worker)
        # Working env for the in-pod toolchain (taskflow / mc sync). FS_* 契约
        # 同 worker-bridge/cimicode-sandbox：绝不设置 AGENTTEAMS_RUNTIME——
        # mc 同步只认 ("k8s","aliyun")，其余走 local 静态三元组模式。
        pod_env: dict[str, str] = {
            "AGENTTEAMS_FS_ROOT": "/workspace",
            "AGENTTEAMS_WORKER_NAME": worker,
            "OPENCODE_PORT": str(self.cfg.cimicode_port),
        }
        if self.cfg.fs_endpoint:
            pod_env["AGENTTEAMS_FS_ENDPOINT"] = self.cfg.fs_endpoint
            pod_env["AGENTTEAMS_FS_BUCKET"] = self.cfg.fs_bucket
        if team:
            pod_env["AGENTTEAMS_TEAM"] = team
        if matrix_user:
            pod_env["AGENTTEAMS_MATRIX_USER_ID"] = matrix_user
        container_env = env_list(pod_env)
        secret_name = self.cimicode_secret_name(worker)
        if with_fs_secret:
            # 凭据不经明文 env：bridge pod env 里的明文复制进 Secret，
            # 这里以 secretKeyRef 引用。
            container_env += [
                client.V1EnvVar(
                    name=FS_ACCESS_KEY_ENV,
                    value_from=client.V1EnvVarSource(
                        secret_key_ref=client.V1SecretKeySelector(
                            name=secret_name, key=SECRET_KEY_ACCESS
                        )
                    ),
                ),
                client.V1EnvVar(
                    name=FS_SECRET_KEY_ENV,
                    value_from=client.V1EnvVarSource(
                        secret_key_ref=client.V1SecretKeySelector(
                            name=secret_name, key=SECRET_KEY_SECRET
                        )
                    ),
                ),
            ]
        if model_config is not None:
            # 模型配置整段（含网关 key）走 Secret + secretKeyRef；明文哈希 env
            # 让内容变更（模型/网关/key 轮转）体现为 pod template 漂移 → 滚动
            # 重启生效（Secret 值变更本身不触发 pod 重启）。
            container_env += [
                client.V1EnvVar(
                    name=ENV_MODEL_CONFIG_HASH,
                    value=hashlib.sha256(model_config.encode("utf-8")).hexdigest()[:16],
                ),
                client.V1EnvVar(
                    name=ENV_MODEL_CONFIG,
                    value_from=client.V1EnvVarSource(
                        secret_key_ref=client.V1SecretKeySelector(
                            name=secret_name, key=SECRET_KEY_MODEL_CONFIG
                        )
                    ),
                ),
            ]
        container = client.V1Container(
            name="cimicode",
            image=self.cfg.cimicode_image,
            image_pull_policy="IfNotPresent",
            env=container_env,
            ports=[
                client.V1ContainerPort(name="runtime", container_port=self.cfg.cimicode_port),
            ],
            # 无 emptyDir/hostPath：/workspace = 容器可写层。pod 重建丢会话由
            # bridge adapter 的 404 自愈 + 每 turn 重推 AGENTS.md 兜住（容器
            # 重启也丢——按部署决策接受，PVC 化仍是生产化步骤）。
        )
        if self.cfg.cimicode_probe_path:
            container.readiness_probe = client.V1Probe(
                http_get=client.V1HTTPGetAction(
                    path=self.cfg.cimicode_probe_path, port=self.cfg.cimicode_port
                ),
                initial_delay_seconds=5,
                period_seconds=10,
            )
        pod_spec = client.V1PodSpec(containers=[container])
        if self.cfg.provision_node_selector:
            pod_spec.node_selector = {
                "kubernetes.io/hostname": self.cfg.provision_node_selector
            }
        return client.V1Deployment(
            metadata=client.V1ObjectMeta(
                name=name,
                namespace=self.cfg.namespace,
                labels=labels_for(worker),
            ),
            spec=client.V1DeploymentSpec(
                replicas=1,
                selector=client.V1LabelSelector(match_labels={"app": name}),
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(labels={"app": name}),
                    spec=pod_spec,
                ),
            ),
        )
```

**hunk 6 — `deployment_drift` 方法**：删除 volumes 比较整段（含 `is not None`
幻影漂移注释），return 改为：

```python
        live_env, live_refs = self._env_fingerprint(lc)
        want_env, want_refs = self._env_fingerprint(dc)
        # 无卷可比（/workspace = 容器可写层）：漂移面 = image + probe + env +
        # secretKeyRef 引用 + nodeSelector。Secret 值变更不重启 pod——模型/
        # 网关/key 轮转靠 CIMICODE_MODEL_CONFIG_HASH 明文 env 变更入 env 指纹。
        return (
            lc.image != dc.image
            or live_probe_path != want_probe_path
            or live_env != want_env
            or live_refs != want_refs
            or live.spec.template.spec.node_selector != desired.spec.template.spec.node_selector
        )
```

**hunk 7 — `svc` 方法**：Service ports 列表删 helper 项，只留：

```python
                ports=[
                    client.V1ServicePort(
                        name="runtime",
                        port=self.cfg.cimicode_port,
                        target_port=self.cfg.cimicode_port,
                    ),
                ],
```

**hunk 8 — `ensure_worker_env` 方法**：`wanted` 字典删第三键：

```python
        wanted = {
            ENV_KEY_ADAPTER: ADAPTER_POD,
            ENV_KEY_BASE_URL: f"http://{svc_dns}:{self.cfg.cimicode_port}",
        }
```

**hunk 9 — `reconcile_worker` 方法体**：FS 凭据检查段之后、
`ensure_secret` 移后、插入模型解析；方法尾日志加 model 字段。将原
"creds = ... self.ensure_secret(...) ... team = ..."整段替换为：

```python
        # bridge pod env: MinIO 凭据与模型网关要素的权威来源（controller 组装
        # 的明文 env）。pod 未起（双候选都 404）→ 本轮推迟，下一轮重试——凭据
        # 到位前建 Secret/Deployment 只会得到一个永远连不上 FS 的 pod。
        bridge_env = self.bridge_pod_env(worker)
        if bridge_env is None:
            log.info(
                "worker %s: bridge pod not found yet — provisioning deferred to next pass",
                worker,
            )
            return
        creds = self.fs_credentials(bridge_env)
        with_fs_secret = False
        if self.cfg.fs_endpoint:
            if not creds:
                log.warning(
                    "worker %s: bridge pod env carries no %s/%s — provisioning "
                    "deferred (taskflow/mc sync needs them)",
                    worker,
                    FS_ACCESS_KEY_ENV,
                    FS_SECRET_KEY_ENV,
                )
                return
            with_fs_secret = True
        else:
            log.warning(
                "worker %s: AGENTTEAMS_FS_ENDPOINT not set — degraded plain-chat "
                "pod (taskflow / mc sync non-functional)",
                worker,
            )
        # 模型注入（与原生 worker/leader 链路同源，见 model_config_content）：
        # 缺模型/缺网关要素 → fail-loud 推迟，不建跑不起 turn 的 pod。
        model_config, problem = self.model_config_content(worker_obj, bridge_env)
        if problem:
            log.error(
                "worker %s: model config unavailable — provisioning deferred (%s)",
                worker,
                problem,
            )
            return
        # Secret：FS 凭据 + 模型配置（均"明文不进 Deployment spec"）。
        secret_data: dict[str, str] = dict(creds) if with_fs_secret else {}
        if model_config is not None:
            secret_data[SECRET_KEY_MODEL_CONFIG] = model_config
        if secret_data:
            self.ensure_secret(self.cimicode_secret_name(worker), worker, secret_data)
        team = self.team_for(worker)
        matrix_user = self.matrix_user_id(worker, worker_obj)
        self.ensure_service(self.svc(worker))
        self.ensure_deployment(
            self.cimicode_deployment(
                worker,
                team=team,
                matrix_user=matrix_user,
                with_fs_secret=with_fs_secret,
                model_config=model_config,
            )
        )
        self.ensure_worker_env(worker, worker_obj)
        log.info(
            "worker %s reconciled (mode=%s svc=%s team=%s fs=%s model=%s)",
            worker,
            mode,
            self.cimicode_svc_name(worker),
            team or "-",
            "secret" if with_fs_secret else "degraded",
            "injected" if model_config is not None else "native-config",
        )
```

**hunk 10 — `run()` 日志行**：

```python
        log.info(
            "worker-bridge-operator starting ns=%s cimicode=%s port=%d interval=%ss",
            self.cfg.namespace,
            self.cfg.cimicode_image or "(CIMICODE_IMAGE unset!)",
            self.cfg.cimicode_port,
            self.cfg.interval,
        )
```

**hunk 11 — 模块 docstring**：同步改三处描述——① Service 行
`<worker>-cimicode-svc (runtime :4096)`（单端口）+ Secret 描述加 model config；
② patch 键只剩两键；③ "The cimicode runtime image ..." 段改为：merged
single-container form: cimicode serve + 全套协作工具链，per-turn agent.md 走
消息体原生 `system` 字段（无 in-pod helper、无第二端口）；④ emptyDir 段改为
"No sandbox pod, no hostPath, no emptyDir: /workspace is the container's
writable layer..."；⑤ 新增 Model injection 段（§2.2 内容的英文版）。

### 6.6 `worker-bridge/operator-cimicode/deploy/operator.yaml`

env 列表删除以下三行，并把端口注释改为单端口说明：

```yaml
            # Port pair pinned by the cimicode-runtime image contract.
            - name: CIMICODE_PORT
              value: "4096"
            - name: CIMICODE_HELPER_PORT
              value: "4097"
```

替换为：

```yaml
            # Port pinned by the cimicode-runtime image contract (agent.md
            # rides the message body system field — no helper port).
            - name: CIMICODE_PORT
              value: "4096"
```

### 6.7 `worker-bridge/bridge/src/cimicode_bridge/runtime/cimicode_pod_adapter.py`

**改动 1 — 模块 docstring + 类构造器**（协议要点段第 4 条、`__init__` 签名）：

```python
"""cimicode-pod adapter：operator 供给的 cimicode pod（opencode 换皮）对接形态。

内部 cimicode 是换皮的 opencode——pod 形态的调用契约按 opencode headless
server（``cimicode serve``）的 REST API 走，与 stateless 的 SSE gateway
方言已分化，本文件是独立实现（不继承 CimicodeAdapter 传输基座）。

协议要点（按 cimicode 源码标定，详见 contract/adapter-contract.md）：
  * 会话：``POST /session`` → 会话对象（顶层 ``id``）；``GET /session`` → 列表
  * 消息：``POST /session/{id}/message`` body ``{"system": ..., "parts":
    [{"type": "text", "text": ...}]}``——服务端阻塞整 turn 才返回（独立长
    超时）；``GET /session/{id}/message`` → ``{info: {id, role, time: {created,
    completed?}, error?}, parts: [...]}`` 列表
  * 完成信号：完成的 assistant 消息带 ``info.time.completed``；失败 turn 带
    ``info.error``（``error.data.message`` 是上游报错原文）。SSE 在近期版本
    不可靠——轮询代替
  * 系统指令：**消息体 ``system`` 字段**（cimicode 原生通道）——持久化在
    User 消息上，LLM 调用时拼进 system prompt（agent.prompt + 指令文件 +
    user.system）；历史消息转换不含该字段，只在当前 turn 生效，团队名单
    变化**当 turn 实时生效**。旧链路（helper POST /agents-md 落 cwd
    AGENTS.md）已随 pod 内 helper 整体退役。

会话绑定：Worker CR 的 runtime.yaml bridge 段不预建会话（pod 形态免绑定）；
adapter 自持会话（首 turn 创建、之后复用、404 后重建——pod 重建自愈）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from cimicode_bridge.events import RuntimeEvent, RuntimeEventKind
from cimicode_bridge.runtime.base import RuntimeCapabilities

logger = logging.getLogger(__name__)


class CimicodePodAdapter:
    """pod 形态：调用 operator 供给的集群内 cimicode（opencode）服务。"""

    name = "cimicode-pod"

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: int = 600,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self._session_id = ""                             # adapter 自持的 cimicode 会话
        self._client: httpx.AsyncClient | None = None     # 长连客户端（懒建）
```

**改动 2 — 整段删除 `_push_agent_md` 方法**（原位于 `health/close` 与
`_ensure_session` 之间）。

**改动 3 — `chat` 方法**（删 `await self._push_agent_md(agent_md)` 行；POST
body 加 `system` 键；日志加 system_bytes）：

```python
        """提交 turn：会话自愈 → 阻塞 POST（agent.md 走消息体 system 字段）→ 轮询完成。

        history 已由调用方折进 user_message（三段式上下文，契约 §4）；
        sandbox 绑定由 base_url 本身承载（单 pod 无独立沙箱）。
        agent_md 经 POST body 的 ``system`` 字段随消息下发——cimicode 原生
        通道，当前 turn 即生效（历史消息转换不含该字段，无重复注入）。
        正常完成返回 [TEXT_DELTA(全文), TURN_COMPLETED]——与 stateless 的
        事件形态同构（聚合器按 turn_completed 收口，全文发一次）。
        """
        del history, sandbox_id
        turn_started = time.monotonic()
        try:
            resolved = await self._ensure_session(session_id)
            baseline = self._last_assistant_id(await self._messages(resolved))
            logger.info(
                "cimicode-pod action: post message session=%s turn=%s system_bytes=%d user_message=%r",
                resolved, turn_id, len(agent_md.encode("utf-8")), user_message[:300],
            )
            response = await self._http().post(
                f"{self.base_url}/session/{resolved}/message",
                json={"system": agent_md, "parts": [{"type": "text", "text": user_message}]},
                # POST 服务端阻塞整 turn——给独立长超时（轮询另有自己的窗口）
                timeout=self.timeout_seconds + 30.0,
            )
```

（`response.raise_for_status()` 之后的轮询/progress/日志/异常处理段不变。）

### 6.8 `worker-bridge/bridge/src/cimicode_bridge/runtime/registry.py`（全文）

```python
"""Runtime SPI 工厂：按 ``runtime.adapter`` 配置分派到具体 adapter 实现。

bridge 核心必须保持 runtime 无关——本工厂是唯一知道"哪个 adapter 类服务
哪个名字"的地方（spec §3.1 Runtime SPI）。两形态传输已分化：
cimicode-stateless 走 SSE gateway（CimicodeAdapter 基座），cimicode-pod
走 opencode REST 轮询（换皮 opencode 的 pod 直连）。
"""
from __future__ import annotations

from cimicode_bridge.config import RuntimeConfig
from cimicode_bridge.runtime.cimicode_pod_adapter import CimicodePodAdapter
from cimicode_bridge.runtime.cimicode_stateless_adapter import CimicodeStatelessAdapter

VALID_ADAPTERS = ("cimicode-stateless", "cimicode-pod")


def build_runtime_adapter(runtime: RuntimeConfig):
    """按配置构建 runtime adapter。

    adapter 为空（未定）或 base_url 为空时抛 ValueError——bridge 装配层
    （app.py）负责在两者齐备后才调用本工厂，绝不静默回落到任何默认地址。
    """
    if not runtime.adapter:
        raise ValueError("runtime adapter undetermined (no BRIDGE_RUNTIME_ADAPTER env, no bridge section)")
    if not runtime.base_url:
        raise ValueError(f"runtime base_url is empty for adapter {runtime.adapter!r}")
    if runtime.adapter == "cimicode-stateless":
        return CimicodeStatelessAdapter(
            runtime.base_url,
            timeout_seconds=runtime.turn_timeout_seconds,
        )
    if runtime.adapter == "cimicode-pod":
        return CimicodePodAdapter(
            runtime.base_url,
            timeout_seconds=runtime.turn_timeout_seconds,
        )
    raise ValueError(f"unknown runtime adapter: {runtime.adapter!r} (expected one of {VALID_ADAPTERS})")
```

（差异：删 docstring 里 helper_url 说明行；`CimicodePodAdapter(...)` 调用删
`helper_url=runtime.helper_url`。）

### 6.9 `worker-bridge/bridge/src/cimicode_bridge/config.py`

`RuntimeConfig` 中删除一行：

```python
    helper_url: str = ""                               # pod 专用：cimicode pod 内 AGENTS.md helper（operator env）
```

### 6.10 `worker-bridge/bridge/src/cimicode_bridge/app.py`（三处）

**改动 1 — `_apply_env_overrides`**：删除三行——

```python
        helper_url_env = os.getenv("BRIDGE_RUNTIME_HELPER_URL", "")
```

```python
        if helper_url_env:
            self.config.runtime.helper_url = helper_url_env
```

**改动 2 — `_recover_late_runtime_wiring` docstring**：
`（BRIDGE_RUNTIME_ADAPTER / _BASE_URL / _HELPER_URL）` →
`（BRIDGE_RUNTIME_ADAPTER / _BASE_URL）`。

**改动 3 — `_recover_late_runtime_wiring` 循环体**：删除 helper_url 读取与
日志字段——

```python
                helper_url = str(runtime_env.get("BRIDGE_RUNTIME_HELPER_URL", ""))
                if helper_url:
                    self.config.runtime.helper_url = helper_url
                logger.info(
                    "late runtime wiring recovered from controller: adapter=%s base_url=%s helper_url=%s",
                    adapter,
                    self.config.runtime.base_url,
                    self.config.runtime.helper_url,
                )
```

替换为：

```python
                logger.info(
                    "late runtime wiring recovered from controller: adapter=%s base_url=%s",
                    adapter,
                    self.config.runtime.base_url,
                )
```

### 6.11 `worker-bridge/bridge/src/cimicode_bridge/bootstrap.py`

整体删除 `runtime_helper_url` property（原位于 `gateway_sandbox_id` 与
`runtime_bridge_section` 之间）：

```python
    @property
    def runtime_helper_url(self) -> str:
        """sandbox AGENTS.md helper 服务地址（legacy opencode 链路用，已退役）。"""
        return str(
            self.bridge_runtime_config.get("helperUrl")
            or self.bridge_runtime_config.get("helper_url")
            or ""
        )
```

### 6.12 `worker-bridge/contract/adapter-contract.md`（重写为 v1.1，全文）

```markdown
# Runtime Adapter 传输契约（cimicode-stateless / cimicode-pod）

**版本** v1.1（2026-09-16）· 对应实现：`worker-bridge/bridge/src/cimicode_bridge/runtime/`

两种 adapter 形态共享 Runtime SPI（`chat(*, session_id, sandbox_id, turn_id,
agent_md, history, user_message) -> list[RuntimeEvent]`，base.py），传输层已分化：

| | cimicode-stateless | cimicode-pod |
|---|---|---|
| 对端 | 外部 cimicode 平台 gateway | operator 供给的 cimicode pod（换皮 opencode） |
| 传输 | HTTP + SSE（`POST /v1/gateway/session/chat`） | cimicode REST + 轮询 |
| agent.md | 请求字段（agentMd） | **消息体 `system` 字段**（cimicode 原生） |
| 会话 | 预建绑定（runtime.yaml bridge 段 sessionId/sandboxId） | adapter 自持（404 自愈重建） |
| 流式 | SSE 事件流 | 否（turn 结束出全文 + progress_texts） |

## cimicode-pod REST 契约（按 agi-opencode 源码标定）

端口（镜像契约，`worker-bridge/cimicode-runtime`）：**单端口 4096**（`cimicode
serve`）。pod 内无 helper——旧形态的 :4097（AGENTS.md 落盘 + /exec）已随
agent.md 通道切换整体退役。

| 端口 | 服务 | 端点 |
|---|---|---|
| 4096 | `cimicode serve` | `POST /session`（创建，返回顶层 `id`）；`GET /session`（列表/健康）；`GET /session/{id}`；`POST /session/{id}/message`（**服务端阻塞整 turn**）；`GET /session/{id}/message`（消息列表） |

turn 生命周期（bridge `CimicodePodAdapter.chat`）：

1. **会话自愈**：入参 session_id（pod 形态恒空）或自持 id `GET` 探活，404 →
   `POST /session` 重建（pod 重建后会话目录丢失是预期内场景）；
2. **baseline**：`GET .../message` 取最后一条 assistant 消息 id；
3. **提交**：`POST .../message` body
   `{"system": <agent_md>, "parts": [{"type": "text", "text": ...}]}`
   ——**agent.md 走 `system` 字段**（独立长超时 = turn 超时 + 30s）；
4. **轮询**：`GET .../message` 直到 baseline 之后出现带 `info.time.completed`
   的 assistant 消息；`info.error`（`error.data.message`）→ RUNTIME_ERROR；
   轮询窗口耗尽 → TURN_INTERRUPTED；
5. **产出**：`[TEXT_DELTA(全文), TURN_COMPLETED]`（与 stateless 事件形态同构）；
   turn 中途的 assistant 插话随 `TURN_COMPLETED.data.progress_texts` 透出。

### agent.md 生效机制（v1.1 变更）

`system` 字段持久化在 User 消息上（`MessageV2.User.system`），LLM 调用时拼进
system prompt（`agent.prompt + 指令文件 + user.system`，`session/llm.ts`）：

- **当前 turn 即生效**——团队名单/协作上下文变化在下一 turn 立即反映，
  旧版"AGENTS.md 变更仅新建会话生效"的限制不复存在；
- **历史消息转换不含该字段**（`toModelMessagesEffect` 只转 parts）——无重复
  注入、无 token 膨胀；
- **fail-loud 且原子**——agent.md 与消息同体提交，不存在"推送成功但 turn
  失败"的中间态；bridge 侧也不再需要 helper_url（`BRIDGE_RUNTIME_HELPER_URL`
  接线键已删除）。

SSE 在近期版本不可靠——轮询是标定结论，勿回退。

## 接线（Worker CR spec.env → bridge 自愈轮询）

operator-cimicode patch 两键（bridge 经 controller runtimeEnv 读取，或 pod env
直接注入）：

```
BRIDGE_RUNTIME_ADAPTER=cimicode-pod
BRIDGE_RUNTIME_BASE_URL=http://<w>-cimicode-svc.<ns>.svc:4096
```

模型注入（与原生 worker/leader 链路同源）：operator-cimicode 从 Worker CR
`spec.model` + bridge pod env（`AGENTTEAMS_AI_GATEWAY_URL` /
`AGENTTEAMS_WORKER_GATEWAY_KEY`）渲染 cimicode 配置方言，经
`OPENCODE_CONFIG_CONTENT` env（secretKeyRef，cimicode 合并优先级最高的配置
源）注入；`spec.model == "native-config"` 跳过注入。详见
`worker-bridge/operator-cimicode/worker_bridge_operator.py` 模块头。

会话门禁（app.py 三处 `== "cimicode-stateless"`）语义是"仅 stateless 要求预建
绑定"——pod 形态免预建，勿放宽为通用检查。

## 已知限制（生产化备忘）

- **会话数据随容器丢失**（无 emptyDir，/workspace = 容器可写层）：pod 重建由
  404 自愈 + 每 turn 重发 system + taskflow `mc pull` 恢复。PVC 化是生产化步骤。
- **ripgrep 必装**（基础镜像已含）：cimicode skill 工具 shell 出 rg，缺则每次
  调用挂 130s。
- 0.5.0 镜像内 cimicode 与 agi-opencode 仓库 HEAD 的版本对齐需冒烟确认
  （`system` 字段、`info.time.completed`/`info.error` 形态）。
```

### 6.13 `Makefile`（两处）

**改动 1 — 本地镜像名区**（`LOCAL_WORKER_BRIDGE_OPERATOR` 行后加一行）：

```makefile
LOCAL_WORKER_BRIDGE_OPERATOR = agentteams/worker-bridge-operator:$(VERSION)
LOCAL_WORKER_BRIDGE_OPERATOR_CIMICODE = agentteams/worker-bridge-operator-cimicode:$(VERSION)
```

**改动 2 — 注释块 + 两个构建目标**：将原
`#   cimicode-runtime ...（ZHIPU_API_KEY build-arg 必填——key 不进仓库）
#   worker-bridge-operator  per-worker cimicode pod 供给器
OPENCODE_VERSION ?= 1.18.27
ZHIPU_API_KEY    ?=`

`build-cimicode-runtime`（含两个 build-arg）与 `build-worker-bridge-operator`
整段替换为：

```makefile
# Standalone worker-bridge 镜像（不进 build: 聚合）：
#   cimicode-runtime     cimicode pod 合并镜像（基于内部 coder-cimicode 基础
#                        镜像 + 协作工具 + skills；模型由 operator 注入，
#                        镜像不烘焙凭据）。原始 opencode 形态备份在
#                        worker-bridge/opencode-runtime/，不再构建。
#   worker-bridge-operator     per-worker cimicode pod 供给器（旧 opencode 形态）
#   worker-bridge-operator-cimicode  cimicode 形态供给器（无 helper 端口 +
#                        模型注入，cimicode pod 相关改动全部落此目录）
CIMICODE_BASE_IMAGE ?= <internal-registry>/library/coder-cimicode:0.5.0

build-cimicode-runtime: ## Build cimicode-runtime merged image (cimicode base + tools + skills)
	@echo "==> Building cimicode-runtime image: $(LOCAL_CIMICODE_RUNTIME)"
	docker build $(PLATFORM_FLAG) $(DOCKER_BUILD_ARGS) \
		--build-arg CIMICODE_BASE_IMAGE=$(CIMICODE_BASE_IMAGE) \
		-f worker-bridge/cimicode-runtime/Dockerfile \
		-t $(LOCAL_CIMICODE_RUNTIME) \
		.

build-worker-bridge-operator: ## Build worker-bridge-operator image
	@echo "==> Building worker-bridge-operator image: $(LOCAL_WORKER_BRIDGE_OPERATOR)"
	# context = operator 目录（Dockerfile 的 COPY 路径相对该目录）
	docker build $(PLATFORM_FLAG) $(DOCKER_BUILD_ARGS) \
		-f worker-bridge/operator/Dockerfile \
		-t $(LOCAL_WORKER_BRIDGE_OPERATOR) \
		worker-bridge/operator

build-worker-bridge-operator-cimicode: ## Build worker-bridge-operator (cimicode variant) image
	@echo "==> Building worker-bridge-operator-cimicode image: $(LOCAL_WORKER_BRIDGE_OPERATOR_CIMICODE)"
	# context = operator-cimicode 目录（cimicode pod 供给器：模型注入 + 无 helper）
	docker build $(PLATFORM_FLAG) $(DOCKER_BUILD_ARGS) \
		-f worker-bridge/operator-cimicode/Dockerfile \
		-t $(LOCAL_WORKER_BRIDGE_OPERATOR_CIMICODE) \
		worker-bridge/operator-cimicode
```

### 6.14 `worker-bridge/README.md`（文档性改动，四个位置）

1. 顶部 `cimicode-pod` 形态描述段：改为 `operator-cimicode/` 供给、单端口、
   Secret 含模型配置、两键 patch、agent.md 走消息体 system 字段；
2. 目录表：`operator/` 行标注"旧 opencode 形态，保留不动"；新增
   `operator-cimicode/` 行（模型注入/无 helper/无卷/两键）；`cimicode-runtime/`
   行更新为新形态描述；新增 `opencode-runtime/` 行（原始备份，不再构建）；
3. 本地验证节：operator 行改 19 个，新增
   `cd ../operator-cimicode && python -m pytest tests -q  # 28 个`。

### 6.15 `changelog/current.md`（追加三条）

```markdown
- feat(worker-bridge): switch cimicode-runtime to the internal coder-cimicode base image — per-turn agent.md now rides the message-body `system` field (cimicode-native, effective same turn), the in-pod sandbox helper (:4097) and bash.ts custom tool are retired; `OPENCODE_PERMISSION={"*":"allow"}` guards against headless permission-ask deadlock; skills load from the image via `skills.paths` (zero copy)
- feat(worker-bridge): add `operator-cimicode/` (authoritative cimicode-pod provisioner) — model injection from Worker CR `spec.model` + bridge pod gateway env via `OPENCODE_CONFIG_CONTENT` secretKeyRef with hash-based rollout; Secret carries FS creds + model config; drop emptyDir and helper port; Worker env patch is now two keys
- refactor(worker-bridge): keep the original opencode-form runtime and operator as unchanged backups under `opencode-runtime/` and `operator/`; drop `ZHIPU_API_KEY`/`OPENCODE_VERSION` build args from `build-cimicode-runtime` (add `CIMICODE_BASE_IMAGE`)
```

---

## 7. 遗留事项与后续优化（非本次范围）

- **prompt cache**：agent_md 逐 turn 重渲染，内容字节稳定时缓存前缀稳定；
  llm.ts:114-125 有 system[0] 的缓存友好切分，暂无额外动作。
- **cimicode build-agent 身份文本**会排在我们的 agent.md 之前（llm.ts:104），
  语义轻微重叠可接受；极致化可用 config `agent` 键定义空 prompt 自定义
  agent + POST 带 `"agent"` 字段压制。
- **PVC 化**工作区（生产化步骤，契约"已知限制"已记）。
- `worker-bridge/cimicode-sandbox/`（stateless 形态自建沙箱基础镜像）本次
  未动，与 pod 形态无依赖关系。
- 设计长文 `worker-bridge/docs/worker-bridge运行时替换与协作流转详解.md`
  等历史文档描述的是旧链路，未随本次改写；传输层权威以
  `contract/adapter-contract.md` v1.1 为准。