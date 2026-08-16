# cimicode 运行时接入分析（基于 opencode 的无状态 code agent）

> 日期: 2026-08-16
> 基线: 分支 `dev-v1.2.2`；目标：新增 Worker 运行时 `cimicode`——基于 opencode（Node/Bun 生态开源终端 coding agent）魔改的 **cimicode 无状态版本**，作为 Worker 加入平台
> 环境前提: K8s Helm 部署 + 外接 S3 endpoint（静态 AK）+ Synapse homeserver
> 关联文档: [migration-v1.1.2-to-dev-v1.2.2.md](migration-v1.1.2-to-dev-v1.2.2.md)（先完成迁移再接入新运行时）
> 参照模式: `hermes/`（"最薄封装外部 agent 运行时"的仓库样板）

---

## 0. 结论

- 改动面可控：走 **hermes 模式**（包装外部运行时 + openclaw.json 桥接 + shell 文件同步基建），controller 侧约 10 个文件。
- 架构推荐：**`opencode serve` 常驻 + Node 桥进程（matrix-js-sdk）**，文件同步直接复用 `shared/lib/worker-file-sync.sh`，wrapper 只写 Matrix 桥 + 进程管理。
- 最大风险不在代码量，在 **coding agent 工作区的 S3 同步风暴**（`.git`/node_modules/构建产物）和 **readiness/重登录协议**的遵守。

---

## 1. 改动清单（文件级检查表）

### 1.1 Controller / Go（必改，核心链路）

| 文件 | 位置 | 改动 |
|------|------|------|
| `agentteams-controller/internal/backend/interface.go` | L34-38, L60-64 | 新增 `RuntimeCimicode = "cimicode"` 常量；`ValidRuntime()` 加分支（唯一的 runtime 合法性校验入口） |
| `internal/backend/kubernetes.go` | ~L259-266（镜像 switch）、~L275-295（WorkingDir switch）、~L743-750（runtime 归一化） | 三处 switch 加 `case RuntimeCimicode`；镜像用 `config.CimicodeWorkerImage`；工作目录走 default 分支（`HOME == /root/agentteams-fs/agents/<name>`，免改） |
| `internal/backend/docker.go` / `sandbox.go` | docker ~L114-122；sandbox ~L149-156 | 同样的镜像 switch 加分支，**三个 backend 必须同步** |
| `internal/config/config.go` | L568-571、L611-614、L628-631（三处 WorkerEnv/镜像配置结构） | 加 `CimicodeWorkerImage`，env `AGENTTEAMS_CIMICODE_WORKER_IMAGE`（默认 `agentteams/agentteams-cimicode-worker:latest`） |
| `helm/agentteams/crds/workers.agentteams.io.yaml` | L25 | **`spec.runtime` 是硬枚举，必须加 `cimicode`，否则 CR 直接被 API server 拒绝**（注意 openhuman 都不在枚举里） |
| `api/v1beta1/types.go` | L181、L699 | 注释更新（cosmetic） |
| `internal/service/deployer.go` | L395（`inlineOwnsSoul`）、L1487-1501（`builtinAgentDir`） | `builtinAgentDir` 加 `case "cimicode": return filepath.Join(baseDir, "cimicode-worker-agent")`；是否像 copaw/hermes 合并 identity 进 SOUL.md 按需 |
| `internal/executor/package.go` | L658-667（`mergeIdentityIntoSoul`） | 若走 SOUL 合并路线则加 runtime 判断 |
| `cmd/agt/create.go` / `update.go` / `worker_cmd.go` | runtime flag 校验/帮助文本 | 跟随 `ValidRuntime()`，主要是帮助文本 |

**明确不用改的**：

- `internal/service/worker_env.go`：env 注入与 runtime 无关（`AGENTTEAMS_WORKER_NAME/MATRIX_TOKEN/FS 凭证/HOME` 对所有 runtime 通用）。
- `internal/agentconfig/generator.go`：openclaw.json 对非 openclaw runtime 已有兼容分支（L186-189），cimicode 直接消费 openclaw.json。
- `internal/service/runtime_config.go`：仅当走 qwenpaw 式 `runtime.yaml` 期望态下发才需要；**推荐走 hermes 式桥接，不用改**。
- `team_controller.go` / `member_reconcile.go` / `worker_controller.go` 里的 `RuntimeQwenPaw` 分支：全是 qwenpaw(leader)/edge 特例，cimicode 作为普通 worker 不涉及；仅确认它落入"非 qwenpaw 非 copaw"分支的行为正确（team_controller ~L830）。
- `provisioner.go` L523"hermes 不自动加入被邀房间"的补偿逻辑对 cimicode 同样适用（controller 会帮 join）。

### 1.2 Helm（必改）

| 文件 | 位置 | 改动 |
|------|------|------|
| `helm/agentteams/values.yaml` | ~L349-360 | `worker.defaultImage.cimicode.repository/tag`（qwenpaw 反而没接进 helm，controller 用内置默认；按 openclaw/copaw/hermes 模式接） |
| `templates/_helpers.tpl` | ~L196-213 | 新增 `{{- define "agentteams.worker.cimicodeImage" }}` |
| `templates/controller/deployment.yaml` | ~L80-87 | 新增 env `AGENTTEAMS_CIMICODE_WORKER_IMAGE` |

### 1.3 Manager 侧模板（必改）

| 位置 | 改动 |
|------|------|
| `manager/agent/cimicode-worker-agent/`（新建） | 至少 `AGENTS.md`（协作上下文，参照 `hermes-worker-agent/AGENTS.md`），可选 `skills/` |
| `manager/scripts/init/start-manager-agent.sh` ~L1070-1074 | 加两行 `bash "$RENDER" .../cimicode-worker-agent`（workspace + image 两份） |
| `manager/scripts/init/upgrade-builtins.sh` | worker builtins 从 `worker-agent` 发布，cimicode 一般不用改，确认即可 |

### 1.4 Worker 镜像（新建顶层 `cimicode/` 包）

参照 `hermes/` 目录结构：`Dockerfile`（Bun/Node 基础镜像 + `mc` 二进制 + 从 controller 镜像 COPY `agt` CLI + COPY `shared/` 到 `/opt/agentteams/scripts`）+ `scripts/cimicode-worker-entrypoint.sh` + wrapper 源码。同步检查根 `Makefile` 的镜像构建目标。

### 1.5 测试 / 脚手架（跟随改）

`test/testutil/fixtures/worker.go`、`cmd/agt/*_test.go`、`api/v1beta1/types_test.go`、`internal/backend/*_test.go`、`internal/service/deployer_test.go` 等含 runtime 枚举的用例；`install/agentteams-apply.sh` / `import.sh` / `verify.sh` 中 runtime 相关引用逐一确认。

---

## 2. Hermes 参照模式：新 wrapper 需要实现的组件

`hermes/src/hermes_worker/` 是"最薄封装外部 agent 运行时"的样板，组件清单：

1. **CLI 入口**（typer）：`--name --fs --fs-key --fs-secret --fs-bucket --sync-interval --install-dir`；entrypoint 把 controller 注入的 env 翻译成参数。
2. **配置对象**：`workspace = /root/agentteams-fs/agents/<name>`（== HOME == MinIO/S3 mirror 根），runtime home 在 `workspace/.hermes`；cimicode 对应 `workspace/.cimicode`（或 opencode 的 XDG 目录）。
3. **FileSync**（全部经 `mc` CLI 子进程）：
   - 启动 `mirror_all()`：S3 `agents/<name>/` 全量拉到 workspace（`--exclude credentials/**`）；`openclaw.json` 不存在则重试 12×5s 后退出；
   - 周期 `pull_all()`：只拉 Manager 管理的（openclaw.json 字段级合并、skills/、shared/、team shared/）；
   - `push_loop()`：mtime 水位 + 内容比对推送 Worker 管理的文件；
   - 外接 S3 + 静态 AK 场景走本地 `mc alias set` 即可（`shared/lib/oss-credentials.sh` 的 STS 路径不涉及）。
4. **Matrix 重登录**（`worker.py::_matrix_relogin`）：从 `agents/<name>/credentials/matrix/password` 读密码 -> `POST /_matrix/client/v3/login` -> 新 access_token/device_id 写回 openclaw.json。**这是每次容器重启拿到新 device、保持房间在线的关键**。
5. **配置桥接**（`bridge.py`）：openclaw.json -> 运行时自己的配置，只重写自己拥有的 key（MATRIX_*、OPENAI_API_KEY/BASE_URL、model 块），用户自定义保留；openclaw.json 变更触发 re-bridge。
6. **entrypoint**：env 校验 -> 时区 -> 凭证 -> readiness reporter 后台子进程（轮询"真正可用"标志，出现后执行 **`agt worker report-ready`** 上报 controller）-> `exec` wrapper。
7. **消息通道**：hermes 自带 Python Matrix adapter；**cimicode 需要自建**（见第 3 节）。

### Node wrapper vs Python wrapper 取舍

- **推荐 Node/Bun wrapper**：cimicode 是 Node/Bun 生态，单语言镜像更薄；Matrix 侧用 `matrix-js-sdk`（openclaw 同款，成熟）。
- **shared/lib 可直接复用**：`worker-file-sync.sh`（自称 runtime-neutral：mtime 水位 + >128 文件降级 `mc mirror` + UTF-8/排除规则）、`oss-credentials.sh`、`agentteams-env.sh`、`mc-wrapper.sh`、`mc-mirror-scope.sh`、`merge-openclaw-config.sh`。**entrypoint 里直接用 bash 起同步循环，wrapper 只负责 Matrix 桥 + 驱动 cimicode 进程**，Node 侧代码量压到最小。
- Python wrapper 唯一优势是可照抄 hermes 的 `sync.py`/技能同步代码，但会给镜像加整个 Python 运行时，不值。

---

## 3. opencode 特有问题与 Matrix 接入架构

> ⚠️ 以下 opencode 细节基于公开常识（撰写时外部检索无有效结果），**动手前务必以 sst/opencode 官方文档与 cimicode 实测核验**：serve API 的 session/message 端点形状、`--session` 续会话语义、XDG 目录覆盖是否完全、config 环境变量展开。

- **交互模式**：默认 TUI；非交互有 `opencode run "<prompt>"`（one-shot，支持 `--session` 续会话、`--mode` 切换）和 `opencode serve`（headless HTTP 服务，暴露 session/message/event API，支持流式）。假设 cimicode 保留了这两条命令。
- **配置**：`opencode.json`；provider 块形如 `{"myprovider": {"npm": "@ai-sdk/openai-compatible", "options": {"baseURL", "apiKey"}, "models": {...}}}`，顶层 `model: "provider/model"`，支持环境变量展开。**OpenAI 兼容 baseUrl+apiKey 是一等公民** -> 与 Higress AI 网关对接没有障碍；桥接逻辑 = openclaw.json 的 `models[*].baseUrl/apiKey/model` -> provider 块 + `model`，照抄 hermes `bridge.py` 的字段解析。
- **IM 集成**：opencode 官方无 Matrix 内置（社区有 Discord/Telegram 类插件），**必须自己做消息通道**：

| 选项 | 做法 | 评价 |
|------|------|------|
| 1（推荐） | `opencode serve` 常驻 + Node 桥（matrix-js-sdk 监听房间 -> serve HTTP API 创建/续 session -> 回复发回房间） | session 保持、可流式、崩溃不丢状态 |
| 2 | 每条消息 `opencode run --session <room>` 子进程 | 实现最简单，但每条消息冷启动、大 workspace 延迟高、并发要自己排队 |
| 3 | 改 cimicode 内部加 Matrix channel | 耦合最深、升级最痛，不建议 |

- **无状态化**：opencode 的 session/消息存储默认在 XDG data 目录（`~/.local/share/opencode`），缓存在 `~/.cache`。容器里 `HOME` 已由 controller 注入 workspace；再把 `XDG_DATA_HOME`/`XDG_CONFIG_HOME` 指到 workspace 子目录，使 session/config 随 S3 同步持久化；`.cache`、`node_modules` 进推送排除表。

---

## 4. 无状态 Worker 的 S3 状态管理

现有机制（`worker-file-sync.sh` + hermes `sync.py`）：mtime 水位增量、hermes 侧文本内容比对、变更文件数 > `AGENTTEAMS_WORKER_SYNC_MIRROR_THRESHOLD`（默认 128）降级整目录 `mc mirror`、排除 credentials/caches/`*.lock`、非 UTF-8 文件名跳过。

对 coding agent 工作区的评估：

1. **够用的部分**：128 文件阈值本质是"改动太多就整树 mirror"，`mc mirror` 只传差异对象，功能上正确；只是同步周期变慢、S3 请求量上升，可通过 env 调大阈值。
2. **要改的部分**：
   - hermes 的 Python 推送是**文本比对**（`read_text(errors="replace")`），对二进制构建产物不可靠；**cimicode 用 shell 版 `worker_sync_push_once`（mtime-only）规避**。
   - **`.git` 目录**：几百~几千个小对象，git 操作后几乎必然 >128 文件触发全树 mirror，对 S3 list/put 压力大。三选一：
     (a) 排除 `.git/**`，代价是重启丢 git 历史；
     (b) **（推荐）代码工作区不用文件级同步持久化 `.git`，改用 git remote（推平台内共享仓库），S3 只同步源码+产物**；
     (c) 心跳时打 tar 快照上传单对象（大 workspace 最省请求）。
   - **node_modules / target / dist**：默认排除；构建产物如需交付，由 agent 显式 `mc cp` 到 `shared/`（现有 file-sharing 技能模式）。
   - 水位文件必须在 workspace 之外（shell 版已做到）；`mc mirror` 不带 `--remove` 不删远端已删文件，长期积累垃圾对象，需定期清理任务。
3. **并发风险**：`shared/` 是 Manager/Worker 单向拉取目录，cimicode 不得写 shared；workspace 内派生文件（opencode.json 桥接产物）必须列入推送排除，否则下一个 pull 周期和 Manager 下发的 openclaw.json 打架（hermes 用 `_HERMES_DERIVED_FILES` 解决，同理处理）。

---

## 5. 风险坑位汇总

1. **CRD 枚举是第一坑**：`workers.agentteams.io.yaml` L25 硬枚举，不加 `cimicode` 则 `kubectl apply` 直接被拒。
2. **三个 backend + config.go 三处结构体的镜像 switch 必须同步改**，漏一处则该 backend 下 cimicode **静默回退到 openclaw 镜像**。
3. **readiness 协议**：controller 依赖 worker 内 `agt worker report-ready` 翻转 Ready；镜像必须 COPY `agt`，并选可靠的"真正可用"信号（如 serve 端口 health check 通过）再上报，否则 Team 编排卡在等待 ready。
4. **Matrix 重登录**：不做 device 级重登录，重启后旧 token 失效/E2EE 异常/房间掉线；照抄 hermes `_matrix_relogin`（密码在 `credentials/matrix/password`）。
5. **房间加入与消息 catch-up**：cimicode 桥要能响应 invite 或依赖 controller 的补偿 join（provisioner.go L523 已兜底）；桥启动后先完成一次 initial sync 再处理新消息，避免首条消息前的 catch-up 丢失（参见 `manager_reconcile_welcome.go` L64 说明）。
6. **S3 对象量与同步风暴**：coding workspace 文件数远超 IM-agent workspace，务必配好排除表 + 阈值 +（可选）tar 快照策略。
7. **opencode 细节需实测验证**（见第 3 节警告）；config 支持环境变量展开的话，网关 apiKey 可通过 env 注入而不落盘。

---

## 6. 建议实施顺序

```
1. 先完成 1.1.2 -> dev-v1.2.2 迁移（见 migration-v1.1.2-to-dev-v1.2.2.md），环境稳定
2. controller 侧 runtime 触点改造 + CRD 枚举（可用一个空壳镜像先打通 CR 创建 -> Pod 调度 -> 配置下发）
3. cimicode 镜像：entrypoint（复用 shared/lib 同步基建）+ agt report-ready + Matrix 重登录
4. Node 桥：matrix-js-sdk <-> opencode serve，session 按房间绑定
5. S3 同步策略调优：排除表（.git/node_modules/桥接产物）+ 阈值 + git remote 方案
6. 加入 Team 编排验证（leader 驱动 coding 任务的端到端流程）
```
