# worker-bridge 功能分支变更详解（feature/worker-bridge-merge vs dev-v1.2.2.p1）— 代码级

> **阅读对象**：需要在本分支上继续开发、部署到内网联调、或做代码评审的工程师。
> **基线**：`origin/dev-v1.2.2.p1`（merge-base `5d949340`）。
> **文档基准**：feature/worker-bridge-merge @ `1418812e`（2026-09-15，已推 origin）。
> **引用约定**：`文件路径:行号` 均为 feature 分支当前代码的真实位置；所有代码块摘自
> 分支源码（长函数有删节时以 `…` 标注）。本文不含任何真实凭据——密钥一律以
> `<占位符>` 表示，实际值只存在于部署命令行 / 集群 Secret 中。

---

## 目录

- [〇、5 分钟总览](#〇5-分钟总览)
- [一、改了什么：分模块代码级详解](#一改了什么分模块代码级详解)
  - [1.1 新增顶层目录 `worker-bridge/`](#11-新增顶层目录-worker-bridge)
  - [1.2 bridge 进程（Python/FastAPI）](#12-bridge-进程pythonfastapi)
  - [1.3 cimicode-runtime 合并镜像](#13-cimicode-runtime-合并镜像)
  - [1.4 worker-bridge-operator（Python）](#14-worker-bridge-operatorpython)
  - [1.5 agentteams-controller（Go）](#15-agentteams-controllergo)
  - [1.6 helm chart](#16-helm-chart)
  - [1.7 CLI / 模板 / 契约文档](#17-cli--模板--契约文档)
- [二、七个 bug 修复：before/after 代码对比](#二七个-bug-修复beforeafter-代码对比)
- [三、放到内网需要注意什么（部署坑全集）](#三放到内网需要注意什么部署坑全集)
- [四、怎么用：从建 Worker 到跑完一个项目](#四怎么用从建-worker-到跑完一个项目)
- [五、怎么改：常见扩展场景的操作手册](#五怎么改常见扩展场景的操作手册)
- [六、测试基线与分支状态](#六测试基线与分支状态)

---

## 〇、5 分钟总览

### 0.1 这个分支做了什么

把三个来源的 worker 运行时统一收敛为 **`worker-bridge` 运行时**：

| 来源 | 原形态 | 去向 |
|---|---|---|
| opencode 双 pod 链路 | worker pod + sandbox pod 两容器栈 | 退役，收敛为 **单 pod 合并镜像** `cimicode-runtime`（opencode + 协作工具同容器） |
| stateless SSE gateway 链路 | bridge 直调外部 cimicode 平台 | 完整保留为 `cimicode-stateless` adapter（SSE 基座不动） |
| copaw 链路 | 自带 Matrix channel 的常驻 agent | 不在本分支改造；作为 team **leader** 与 worker-bridge worker 混编 |

规模：19 个提交（无 merge）、154 文件、+22352/−140。内部 cimicode = 换皮 opencode，
pod 形态按 **opencode 1.18.27** 的 headless REST 契约对接。

### 0.2 供给链全景（谁在什么时候写什么）

```
Manager 在 team room 发 "@<worker> ……"
   │
   ▼
┌─ bridge pod（controller 建，名 agentteams-worker-<w>-bridge）────────────┐
│ cimicode_bridge/app.py（FastAPI + Matrix 长轮询）                        │
│  ├ matrix/filter.py   mention 过滤（无 @ → not_mentioned 丢弃）          │
│  ├ prompt.py          每 turn 从 S3 runtime.yaml 重渲染 agent.md         │
│  ├ runtime/registry.py  adapter 裁决（env > runtime.yaml bridge 段）     │
│  │    ├ CimicodeStatelessAdapter → 外部 cimicode 平台（SSE）             │
│  │    └ CimicodePodAdapter      → 集群内 cimicode pod（REST 轮询）       │
└──────────────────────────────────────────────────────────────────────────┘
   │ BRIDGE_RUNTIME_BASE_URL = http://<w>-cimicode-svc.<ns>.svc:4096
   │ BRIDGE_RUNTIME_HELPER_URL = …:4097
   ▼
┌─ cimicode pod（operator 建，Deployment <w>-cimicode + svc <w>-cimicode-svc）┐
│ 单容器：opencode serve :4096（对话/turn）+ sandbox helper :4097            │
│   helper：POST /agents-md（落 AGENTS.md）/ POST /exec（bash 工具转发）     │
│   工具链：taskflow / agentteams-sync / mc / jq / git / skills              │
│   /workspace=emptyDir；凭据走 Secret <w>-cimicode-fs（secretKeyRef）       │
└────────────────────────────────────────────────────────────────────────────┘
   │ mc 同步（FS_ENDPOINT/BUCKET/凭据）
   ▼
MinIO  teams/<t>/shared/{tasks/,projects/,agents/<w>/agent-md/}
```

**四条供给链各自写的东西**（排查"谁写错了"时按此分工）：

| 组件 | 写什么 | 写到哪 |
|---|---|---|
| helm | `AGENTTEAMS_WORKER_BRIDGE_IMAGE` env | controller Deployment |
| controller | bridge pod + env 三键初值 + runtime.yaml 顶层 `bridge` 段 | Worker CR spec.env、MinIO `agents/<w>/runtime/runtime.yaml` |
| operator | cimicode Deployment/svc/Secret + 回写 env 三键 | 集群对象、Worker CR spec.env |
| bridge | 每 turn agent.md、since 持久化、agent-md 留档 | MinIO、StateStore |

### 0.3 提交清单（新 → 旧）

| 提交 | 类型 | 主题 |
|---|---|---|
| `1418812e` | docs | 变更详解文档（初版；本文为其代码级重写） |
| `da22acf4` | fix(controller) | Failed Team/Human 退避 guard 改为重排剩余窗口（重试链死亡修复） |
| `f949adae` | fix(bridge) | cimicode-pod progress 按位置截断 baseline |
| `781e149d` | fix(operator) | deployment_drift 卷比较改 `is not None` |
| `63578ceb` | fix(cimicode-runtime) | mc 改 vendored 二进制 |
| `193b4166` | fix(make) | operator 镜像构建上下文改 operator 目录 |
| `75c61611` | feat(operator) | cimicode-pod 真实运行时供给 |
| `daf4b28f` | feat(cimicode-runtime) | 合并镜像 |
| `43d115c3` | feat(bridge) | pod adapter 重写为 opencode REST 轮询 |
| `998cc8b7` | refactor | bridge-runtime 并入 bridge |
| `73abde92` | Revert | minio service.type 的 revert |
| `d596b075` | fix(helm) | minio service 尊重 values 的 service.type（后被 revert） |
| `e997987e` | feat | 角色后缀式命名（bridge pod 加 `-bridge`） |
| `b67316bd` | refactor | cimicode-bridge 并入 worker-bridge |
| `ba367f11` | fix(controller) | cluster init registerAdmin 补重试 |
| `bfd04647` | feat(helm) | worker-bridge 镜像供数链（双空 fail-fast）+ CRD 同步 |
| `5ea2e100` | feat(controller) | worker-bridge 运行时统一接入（37 文件，最大提交） |
| `f505bb8f` | feat(operator) | operator 初版（按 adapterMode 分派） |
| `cc531565` | feat | 运行时资产落盘（CLI/模板/契约/文档/sandbox） |
| `a8fd9dca` | feat(bridge) | cimicode-bridge 落盘并统一为双 adapter |

（`6b11e2d5` 适配qwenpaw 为同事提交，只动 `qwenpaw/`，与 worker-bridge 无交集。）

---

## 一、改了什么：分模块代码级详解

### 1.1 新增顶层目录 `worker-bridge/`

```
worker-bridge/
├── bridge/                        # bridge 进程（Python/FastAPI，"冒充" worker 的 Matrix 代理）
│   ├── Dockerfile                 #   构建上下文 = 仓库根（COPY 路径带 worker-bridge/ 前缀）
│   ├── generate_agent_md.py       #   v2.4 生成器本体（runtime.yaml+Persona → agent.md；
│   │                              #   Dockerfile 装到 /opt/agenttools/，env BRIDGE_GENERATOR_PATH 指向）
│   ├── agentteams_log.py          #   CLI 统一 JSONL 日志（同装 /opt/agenttools/）
│   ├── src/cimicode_bridge/
│   │   ├── app.py                 #   602 行主链路（本节 1.2）
│   │   ├── config.py              #   Pydantic 配置模型（env 键总表）
│   │   ├── bootstrap.py           #   S3 拉取 openclaw.json / runtime.yaml / SOUL/PROFILE
│   │   ├── prompt.py              #   调 generator 渲染 agent.md（fail-loud）
│   │   ├── events.py              #   RuntimeEvent / RuntimeEventKind 事件类型
│   │   ├── controller/client.py   #   controller API（401 刷新 / runtimeEnv 查询）
│   │   ├── matrix/filter.py       #   mention 过滤（决策含 reason/role）
│   │   ├── matrix/gateway.py      #   Matrix 收发（sync 长轮询 / since 持久化）
│   │   ├── runtime/registry.py    #   adapter 工厂（唯一知道 adapter 名→类映射的地方）
│   │   ├── runtime/base.py        #   RuntimeCapabilities 契约类型
│   │   ├── runtime/cimicode_stateless_adapter.py   # SSE 形态（外部平台）
│   │   ├── runtime/cimicode_pod_adapter.py         # REST 轮询形态（本分支重写）
│   │   ├── store/{memory,file,redis}.py            # since 持久化后端
│   │   └── api/                   #   /healthz /status 探针路由
│   └── tests/ + tests/unit/       #   生成器 golden + 进程 pytest（115 用例）
├── cimicode-runtime/              # cimicode pod 合并镜像（单容器承载对话+执行）
│   ├── Dockerfile                 #   构建上下文 = 仓库根
│   ├── entrypoint.sh              #   发布 skills → 种子配置 → 起 helper → exec opencode
│   ├── sandbox_helper.py          #   :4097，/healthz /agents-md /exec（纯 stdlib）
│   ├── opencode.json              #   智谱 provider，apiKey=__ZHIPU_API_KEY__ 占位
│   ├── tools/bash.ts              #   同名 custom tool 顶掉内置 bash → 转发 helper /exec
│   └── bin/mc                     #   vendored mc 二进制（RELEASE.2025-08-13，linux-amd64）
├── operator/
│   ├── worker_bridge_operator.py  # 637 行，单文件 operator（本节 1.4）
│   ├── deploy/operator.yaml       #   Deployment+RBAC 范本
│   └── tests/                     #   19 用例
├── cli/                           # taskflow / sync / projectflow 三目录（部署后命令名
│                                  #   taskflow / agentteams-sync / projectflow）+ JSONL 日志
├── template/
│   ├── worker-bridge-agent/       # worker 侧 AGENTS.md 源模板 + skills（部署进镜像）
│   └── worker-bridge-leader-agent/# leader（copaw）侧模板 + skills
├── contract/
│   ├── interface-contract.md      # v2.4 协作接口契约（任务协议/文件流）
│   ├── controller-handover.md     # v2.4 controller 交接契约
│   └── adapter-contract.md        # 两形态传输契约/端口/接线三键/已知限制
├── cimicode-sandbox/              # 历史形态构建上下文（合并镜像落成前的前身，保留参考）
└── docs/                          # 两份设计文档 + 本文
```

### 1.2 bridge 进程（Python/FastAPI）

#### 1.2.1 消息主链路 `handle_matrix_message`（app.py:430）

一条 Matrix 消息进来后的完整路径——**这是读 bridge 代码的入口函数**：

```python
# worker-bridge/bridge/src/cimicode_bridge/app.py:430
async def handle_matrix_message(
    self, room_id: str, sender: str, event_id: str, content: dict[str, Any],
) -> None:
    body = str(content.get("body", ""))
    decision = self.mention_filter.evaluate(body, sender, content=content)
    # 全量决策日志（含丢弃）：原因/角色/mentions 可见
    logger.info(
        "matrix message room=%s sender=%s event=%s accepted=%s role=%s reason=%s mentions=%s body=%r",
        room_id, sender, event_id, decision.accepted, decision.role, decision.reason,
        decision.mentions, body[:80],
    )
    if not decision.accepted:
        # 白名单内的非 mention 消息进 room buffer（群聊视野）
        if decision.reason == "not_mentioned" and decision.role in self.mention_filter.allowed_roles:
            self.history_manager.record_ambient(room_id, sender, body, event_id=event_id)
        return
    …
    # session 绑定缺失（仅 cimicode-stateless 需要）→ 拒绝处理
    if self.config.runtime.adapter == "cimicode-stateless" and (
        not self.config.runtime.session_id or not self.config.runtime.sandbox_id
    ):
        logger.error("Gateway session binding is missing from S3 configuration")
        return
    # CoPaw 三段式群聊视野（history buffer + 当前消息）
    user_message = self.history_manager.build_context(room_id, sender, body)
    await self.matrix_gateway.start_typing(room_id)
    …
```

agent.md 的组装是 **每 turn 重拉 S3**（app.py:483-516，不是启动缓存一次）——
controller 会在成员变化后继续 enrich runtime.yaml，启动时的 bootstrap 缓存会过期：

```python
# app.py:483（节选）
turn_files = self.worker_files
if self.s3_bootstrap is not None:
    fresh = self.s3_bootstrap.load(retries=1)
    if fresh is not None and fresh.runtime_yaml:
        turn_files = fresh
        self.worker_files = fresh
    else:
        logger.warning("per-turn bootstrap pull failed; falling back to boot-time cache")
…
try:
    agent_md = build_agent_md_via_generator(
        runtime_yaml=turn_files.runtime_yaml,
        soul_md=turn_files.soul_md,
        profile_md=turn_files.profile_md,
    )
except GenerateAgentMdError as exc:
    logger.error("agent.md generation failed: %s", exc)
    return                       # fail-loud：渲染失败拒轮，不发半配置 prompt
# agent.md 回写 S3 留档（观测通道，best-effort）
key = self.s3_bootstrap.publish("agent-md/latest.md", agent_md)
```

adapter 调用与事件聚合（app.py:518-547）——**两种 adapter 返回同构事件流**，
聚合器无需感知形态：

```python
# app.py:519
events = await self.runtime_client.chat(
    session_id=self.config.runtime.session_id,
    sandbox_id=self.config.runtime.sandbox_id,
    turn_id=event_id,            # turnId = Matrix event_id（幂等键）
    agent_md=agent_md,
    history=[],                  # history 已折进 user_message（三段式）
    user_message=user_message,
)
response_text = ""
progress_texts: list[str] = []
for event in events:
    if event.kind.value == "text_delta":
        response_text += event.text
    elif event.kind.value == "turn_completed":
        response_text = event.text or response_text      # done.content 权威全文
        progress_texts = list((event.data or {}).get("progress_texts") or [])
    elif event.kind.value in {"runtime_error", "turn_interrupted"}:
        logger.error("Gateway turn failed: %s", event.data or event.text)
        # 失败可见化：告诉房间发生了什么，turn 可被重试
        try:
            reason = (event.text or "runtime error").strip()
            await self.matrix_gateway.send_text(room_id, f"**turn failed**: {reason}")
        except Exception:
            logger.exception("failed to report turn failure to room %s", room_id)
        return
```

progress（worker 中途叙述）先逐条发，最终回复最后发；`NO_REPLY` 标记静默
（app.py:550-566）。**排障时在 bridge pod 日志里搜这些锚点**：

| 日志行 | 出处 | 含义 |
|---|---|---|
| `matrix message … accepted=False reason=not_mentioned` | app.py:446 | 消息被过滤（没 @ 本 worker） |
| `turn start event_id=… adapter=…` | app.py:469 | 进入 turn |
| `agent.md generated bytes=… sha256=…` | app.py:509 | 系统提示已渲染 |
| `Gateway turn failed: …` | app.py:537 | turn 失败（房间也会收到 `**turn failed**`） |
| `no reply event_id=… (empty or NO_REPLY marker)` | app.py:565 | 模型选择不回话（正常） |
| `turn completed event_id=… elapsed=…` | app.py:567 | turn 结束 |

#### 1.2.2 adapter 裁决：`_resolve_adapter_mode`（app.py:195）

**唯一权威顺序**，启动与自愈轮询共用同一函数：

```python
# app.py:195
def _resolve_adapter_mode(self) -> str:
    """① 显式 BRIDGE_RUNTIME_ADAPTER env——operator patch（pod 模式接线）
          或部署层手动覆盖，最高优先；
        ② runtime.yaml 顶层 bridge.adapterMode——controller 投影；
        ③ 都没有 → 空串（未定态）：不建 runtime client，自愈轮询每 15s 重查。
    """
    explicit = os.getenv("BRIDGE_RUNTIME_ADAPTER", "")
    if explicit:
        return explicit
    if self.worker_files is not None:
        mode = self.worker_files.bridge_adapter_mode
        if mode:
            return mode
    return ""
```

配套的 `_build_runtime_client`（app.py:213）执行 fail-loud：adapter 或
base_url 任一为空就返回 `None`（不建 client），**绝不静默回落默认地址**——
旧版写死 `http://cimicode-gateway` 的 mock 残留已清除：

```python
# app.py:219（节选）
mode = self._resolve_adapter_mode()
if not mode:
    logger.warning("runtime adapter undetermined (no env, no bridge section); leaving client unbuilt")
    return None
self.config.runtime.adapter = mode
if not self.config.runtime.base_url:
    logger.warning("runtime base_url missing for adapter %r; leaving client unbuilt", mode)
    return None
```

#### 1.2.3 env 覆盖与 runtime.yaml bridge 段的优先级（app.py:147/169）

```python
# app.py:147（节选）——启动时读 pod env
def _apply_env_overrides(self) -> None:
    adapter_env = os.getenv("BRIDGE_RUNTIME_ADAPTER", "")
    base_url_env = os.getenv("BRIDGE_RUNTIME_BASE_URL", "")
    helper_url_env = os.getenv("BRIDGE_RUNTIME_HELPER_URL", "")
    turn_timeout = os.getenv("BRIDGE_RUNTIME_TURN_TIMEOUT", "")
    if turn_timeout.isdigit() and int(turn_timeout) > 0:
        self.config.runtime.turn_timeout_seconds = int(turn_timeout)
    if adapter_env:
        self.config.runtime.adapter = adapter_env
    if base_url_env:
        self.config.runtime.base_url = base_url_env
        self._explicit_base_url = True      # runtime.yaml 不得遮蔽显式 env
    if helper_url_env:
        self.config.runtime.helper_url = helper_url_env
```

绑定优先级（`_apply_bridge_section`，app.py:169）：

```
显式 env（BRIDGE_RUNTIME_*，排障/覆盖用）
  > runtime.yaml 顶层 bridge 段（controller 从 Worker CR spec 投影）
  > legacy openclaw.json bridge.runtime 段（兼容兜底）
  > 无
```

#### 1.2.4 晚到接线自愈：`_recover_late_runtime_wiring`（app.py:273）

**解决的真实问题**：bridge pod 可能先于 operator 回写 env 三键被创建——
controller 在写入 spec.env 后**不会滚动 pod**，进程启动时的 env 是残缺的。
与其重建 pod，不如 bridge 自己每 15s（`RECOVERY_POLL_SECONDS`）轮询
controller `GET /api/v1/workers/{self}` 直到 runtimeEnv 携带 adapter，然后
**进程内**重建 runtime adapter 与 Matrix 网关：

```python
# app.py:338（节选）
runtime_env = await self._fetch_runtime_env(worker, controller_url)
adapter = str(runtime_env.get("BRIDGE_RUNTIME_ADAPTER", ""))
if adapter and adapter != last_adapter:
    last_adapter = adapter
    base_url = str(runtime_env.get("BRIDGE_RUNTIME_BASE_URL", ""))
    if base_url:
        self.config.runtime.base_url = base_url
        self._explicit_base_url = True
    helper_url = str(runtime_env.get("BRIDGE_RUNTIME_HELPER_URL", ""))
    if helper_url:
        self.config.runtime.helper_url = helper_url
    …
    self.runtime_client = self._build_runtime_client()
    self.matrix_gateway = self._build_matrix_gateway()
    if self.matrix_gateway is not None:
        self.matrix_task = asyncio.create_task(self.matrix_gateway.start())
        …
        if self.phase == "listening":
            logger.info("bridge recovered to listening without a pod restart (adapter=%s)", adapter)
            return
```

轮询还顺带兜住了另一类卡死（r2 修复）：早于 controller 推送
`agents/<w>/runtime/runtime.yaml` 启动的 bridge，每轮重试 S3 bootstrap，
落地后在进程内重建网关（app.py:301-337）。

**冷启动补消费**：gateway 重建走的是同一 `start()`，initial sync 会从持久化的
`since` token 回放——挂机期间错过的 mention 消息不会丢（105 第七轮实测：
p2-qa bridge Pending 一夜，起 pod 后自动补消费错过的分派，零丢失）。

#### 1.2.5 adapter 工厂 `runtime/registry.py`（全文 41 行）

```python
# worker-bridge/bridge/src/cimicode_bridge/runtime/registry.py:14
VALID_ADAPTERS = ("cimicode-stateless", "cimicode-pod")

def build_runtime_adapter(runtime: RuntimeConfig):
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
            helper_url=runtime.helper_url,
            timeout_seconds=runtime.turn_timeout_seconds,
        )
    raise ValueError(f"unknown runtime adapter: {runtime.adapter!r} (expected one of {VALID_ADAPTERS})")
```

加新 adapter 时这里是必改点之一（见 §5.1）。

#### 1.2.6 `CimicodePodAdapter`（runtime/cimicode_pod_adapter.py，321 行）

模块头注释就是协议契约的浓缩（`runtime/cimicode_pod_adapter.py:1-21`）——
**按 opencode 1.18.27 标定**：

```python
"""cimicode-pod adapter：operator 供给的 cimicode pod（opencode 换皮）对接形态。

协议要点（按 opencode 1.18.27 标定，详见 contract/adapter-contract.md）：
  * 会话：``POST /session`` → 会话对象（顶层 ``id``）；``GET /session`` → 列表
  * 消息：``POST /session/{id}/message`` body ``{"parts": [{"type": "text",
    "text": ...}]}``——服务端阻塞整 turn 才返回（独立长超时）；
    ``GET /session/{id}/message`` → ``{info: {id, role, time: {created,
    completed?}, error?}, parts: [...]}`` 列表
  * 完成信号：完成的 assistant 消息带 ``info.time.completed``；失败 turn 带
    ``info.error``（``error.data.message`` 是上游报错原文）。SSE 在近期版本
    不可靠——轮询代替
  * 系统指令：opencode 从工作目录读 ``AGENTS.md``；bridge 每 turn 重拼的
    agent.md 经 pod 内 helper（``POST {helper_url}/agents-md``）推送，随后可
    复用会话（AGENTS.md 变更在新建会话时生效）

会话绑定：Worker CR 的 runtime.yaml bridge 段不预建会话（pod 形态免绑定）；
adapter 自持会话（首 turn 创建、之后复用、404 后重建——pod 重建自愈）。
"""
```

**chat() 主流程**（`cimicode_pod_adapter.py:246`）——五步：推 agent.md →
会话自愈 → 记 baseline → 阻塞 POST → 轮询完成：

```python
# cimicode_pod_adapter.py:246（节选，注释保留原文）
async def chat(
    self, *, session_id: str, sandbox_id: str, turn_id: str,
    agent_md: str, history: list[dict[str, Any]], user_message: str,
) -> list[RuntimeEvent]:
    del history, sandbox_id          # history 已折进 user_message；sandbox 由 base_url 承载
    turn_started = time.monotonic()
    try:
        await self._push_agent_md(agent_md)
        resolved = await self._ensure_session(session_id)
        baseline = self._last_assistant_id(await self._messages(resolved))
        response = await self._http().post(
            f"{self.base_url}/session/{resolved}/message",
            json={"parts": [{"type": "text", "text": user_message}]},
            # POST 服务端阻塞整 turn——给独立长超时（轮询另有自己的窗口）
            timeout=self.timeout_seconds + 30.0,
        )
        response.raise_for_status()
        completed = await self._poll_reply(resolved, baseline)
        if completed.kind == RuntimeEventKind.TURN_COMPLETED:
            # 把 turn 中途的 assistant 进度叙述（工具调用之间的插话）
            # 随完成事件透出——调用方先发 progress 再发正式回复。
            texts = self._completed_assistant_texts(await self._messages(resolved), baseline)
            progress = [t for t in texts[:-1] if t.strip() and t.strip() != completed.text]
            if progress:
                completed = RuntimeEvent(
                    kind=RuntimeEventKind.TURN_COMPLETED,
                    text=completed.text,
                    data={**(completed.data or {}), "progress_texts": progress},
                )
        …
        if completed.kind == RuntimeEventKind.TURN_COMPLETED:
            return [
                RuntimeEvent(kind=RuntimeEventKind.TEXT_DELTA, text=completed.text),
                completed,
            ]            # 与 stateless 事件形态同构（app.py 聚合两形态共用）
        return [completed]
```

**agent.md 推送**（`cimicode_pod_adapter.py:82`）——helper_url 缺失宁可报错：

```python
# cimicode_pod_adapter.py:82
async def _push_agent_md(self, agent_md: str) -> None:
    """把本 turn 重拼的 agent.md 推到 pod 内 helper（落 AGENTS.md）。"""
    if not self.helper_url:
        # fail-loud：helper_url 缺失说明 operator 接线不完整，宁可报错
        # 也不要静默跳过（否则 opencode 用旧系统指令跑 turn）。
        raise RuntimeError("cimicode-pod adapter requires helper_url (BRIDGE_RUNTIME_HELPER_URL)")
    response = await self._http().post(
        f"{self.helper_url}/agents-md",
        content=agent_md.encode("utf-8"),
        headers={"Content-Type": "text/plain; charset=utf-8"},
    )
    response.raise_for_status()
```

**会话自愈**（`cimicode_pod_adapter.py:96`）——pod 重建后会话目录丢失是预期场景：

```python
# cimicode_pod_adapter.py:96（节选）
async def _ensure_session(self, session_id: str) -> str:
    if not session_id:
        session_id = self._session_id
    if session_id:
        response = await self._http().get(f"{self.base_url}/session/{session_id}")
        if response.status_code == 200:
            self._session_id = session_id
            return session_id
        logger.warning("cimicode-pod session %s vanished; recreating", session_id)
    response = await self._http().post(f"{self.base_url}/session", json={})
    response.raise_for_status()
    …
```

**轮询三态**（`cimicode_pod_adapter.py:199`）：completed（正常完成）/
error（`info.error` → RUNTIME_ERROR，`error.data.message` 带上游原文）/
deadline（超时 → TURN_INTERRUPTED）。轮询窗口按 baseline **位置截断**（见 §2.1）。

超时的语义特意区分（`cimicode_pod_adapter.py:307-318`）——阻塞 POST 放弃
≠ turn 死亡，opencode 侧通常仍在跑，报错文案带上这层语义
`"(task still running server-side; ask again to re-attach)"`。

**排障锚点**（bridge 日志，cimicode_pod_adapter.py 内）：
`push agent.md to helper=… bytes=…`（:88）、`reuse session id=…` / `created
session id=…`（:105/:120）、`session … vanished; recreating`（:107）、
`post message session=… turn=… user_message=…`（:269）、`turn finished
session=… outcome=… elapsed=… progress=N reply=…`（:292）。

#### 1.2.7 bridge 配置模型与 env 键总表（config.py）

`RuntimeConfig`（config.py:39）关键字段——**adapter/base_url/helper_url 故意
空默认**，合法来源只有 runtime.yaml bridge 段与 `BRIDGE_RUNTIME_*` env：

```python
# config.py:39（节选）
class RuntimeConfig(BaseModel):
    adapter: str = ""              # cimicode-stateless / cimicode-pod（空=未定）
    base_url: str = ""             # stateless 取 bridge 段；pod 取 operator env
    helper_url: str = ""           # pod 专用：AGENTS.md helper（operator env）
    template_id: str = ""          # 仅来自 CR 投影，无假默认
    session_id: str = ""           # S3 下发的 gateway session
    sandbox_id: str = ""           # S3 下发的 sandbox
    auth_type: str = "none"        # gateway 当前不鉴权
    turn_timeout_seconds: int = 3600   # 长 turn 等待上限（BRIDGE_RUNTIME_TURN_TIMEOUT 可覆盖）
```

bridge 进程消费的全部 env（排障按名搜）：

| env | 来源 | 用途 |
|---|---|---|
| `BRIDGE_RUNTIME_ADAPTER` | operator patch / 部署覆盖 | adapter 形态（最高优先） |
| `BRIDGE_RUNTIME_BASE_URL` | operator patch | opencode svc 地址 |
| `BRIDGE_RUNTIME_HELPER_URL` | operator patch | helper svc 地址 |
| `BRIDGE_RUNTIME_TURN_TIMEOUT` | 可选 | turn 超时秒数覆盖 |
| `AGENTTEAMS_WORKER_NAME` | controller | 自愈轮询查 controller 的 worker 名 |
| `AGENTTEAMS_CONTROLLER_URL` | controller | 自愈轮询的 API 地址 |
| `AGENTTEAMS_MATRIX_URL` | controller | Synapse 地址（homeserver 占位符解析） |
| `AGENTTEAMS_WORKER_MATRIX_USER_ID` | controller | mention 匹配预判（whoami 后被真值覆盖） |
| `COORDINATION_LEADER/ADMIN/WORKERS` | controller | 角色解析（过滤白名单） |
| `AGENTTEAMS_WORKER_MATRIX_TOKEN` | 本地开发 | token 兜底（线上走 S3） |
| `BRIDGE_REDIS_URL` | 可选 | redis StateStore（默认 memory） |
| `BRIDGE_LOG_FILE` | 可选 | 容器内轮转日志文件 |

### 1.3 cimicode-runtime 合并镜像

#### 1.3.1 Dockerfile（全文结构，`worker-bridge/cimicode-runtime/Dockerfile`）

```dockerfile
FROM node:22-slim

ARG OPENCODE_VERSION=1.18.27
ARG NPM_REGISTRY=https://registry.npmjs.org
ARG ZHIPU_API_KEY

RUN apt-get update \
    # ripgrep: opencode 的 skill 工具 shell 出 rg 扫描 skill 内容——缺则每次
    # 调用挂 130s 后报 "ripgrep execution failed"（node-slim 不带 rg）；
    # python3 承载 helper（纯 stdlib，无需 pip）；其余为协作工具运行依赖。
    && apt-get install -y --no-install-recommends \
        ca-certificates tini ripgrep python3 curl jq git procps \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g --registry="${NPM_REGISTRY}" "opencode-ai@${OPENCODE_VERSION}" \
    && npm cache clean --force

# mc（MinIO 客户端）：vendored 二进制直供，不走构建期下载——dl.min.io 的
# latest 式路径已下线（410），且真实内网构建无外网。
COPY worker-bridge/cimicode-runtime/bin/mc /usr/local/bin/mc
RUN chmod +x /usr/local/bin/mc

COPY worker-bridge/cimicode-runtime/sandbox_helper.py /opt/agentteams/sandbox_helper.py
COPY worker-bridge/cimicode-runtime/opencode.json /opt/agentteams/opencode.json
COPY worker-bridge/cimicode-runtime/tools/bash.ts /opt/agentteams/tools/bash.ts
# 烘焙 apiKey：build-arg 必填，缺则构建失败；占位符必须被完全替换。
RUN if [ -z "${ZHIPU_API_KEY}" ]; then echo "ZHIPU_API_KEY build-arg is required" >&2; exit 1; fi \
    && sed -i "s|__ZHIPU_API_KEY__|${ZHIPU_API_KEY}|" /opt/agentteams/opencode.json \
    && ! grep -q "__ZHIPU_API_KEY__" /opt/agentteams/opencode.json

COPY worker-bridge/cimicode-runtime/entrypoint.sh /opt/agentteams/entrypoint.sh
# sed 剥 entrypoint 的 CR：CRLF shebang（#!/bin/sh\r）会让 exec 误报
# no such file or directory——Windows 编辑过的文件不得打断启动。
RUN sed -i "s|\r$||" /opt/agentteams/entrypoint.sh \
    && chmod +x /opt/agentteams/entrypoint.sh /opt/agentteams/sandbox_helper.py

# skills 全套 + 协作协议 CLI（全局命令包装到 PATH）
COPY worker-bridge/template/worker-bridge-agent/skills /opt/agentteams/skills
RUN printf '#!/bin/sh\nexec python3 /opt/agentteams/skills/task-management/scripts/taskflow.py "$@"\n' > /usr/local/bin/taskflow \
    && printf '#!/bin/sh\nexec python3 /opt/agentteams/skills/file-sharing/scripts/agentteams_sync.py "$@"\n' > /usr/local/bin/agentteams-sync \
    && chmod +x /usr/local/bin/taskflow /usr/local/bin/agentteams-sync \
    && chmod +x /opt/agentteams/skills/*/scripts/*.py || true

ENV AGENTTEAMS_SKILLS_ROOT=/opt/agentteams/skills \
    OPENCODE_PORT=4096 \
    BRIDGE_SANDBOX_HELPER_PORT=4097
EXPOSE 4096 4097
WORKDIR /workspace
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/agentteams/entrypoint.sh"]
```

**注意构建上下文 = 仓库根**（所有 COPY 带 `worker-bridge/` 前缀）——与 operator
镜像相反，见 §3.1。

#### 1.3.2 entrypoint.sh（全文 42 行）

```sh
#!/bin/sh
#   1. 把镜像内 skill 树发布到工作目录的 .opencode/skills/（opencode 以
#      cwd 原生发现 project skills，skill 工具才可用）；
#   2. 种子 opencode 全局配置（智谱 provider + 转发 bash 工具）到 $HOME；
#   3. 后台起 sandbox helper（:4097）；
#   4. 前台 exec opencode serve（:4096，bridge 的 turn 入口）。
set -eu

WORKDIR="${AGENTTEAMS_FS_ROOT:-/workspace}"
OPENCODE_PORT="${OPENCODE_PORT:-4096}"
HELPER_PORT="${BRIDGE_SANDBOX_HELPER_PORT:-4097}"

mkdir -p "$WORKDIR"
cd "$WORKDIR"
export AGENTTEAMS_FS_ROOT="$WORKDIR"
export OPENCODE_WORKDIR="$WORKDIR"

# 发布 skill 树为 opencode project skills（镜像内副本权威，每次启动重同步）
if [ -d /opt/agentteams/skills ]; then
    mkdir -p "$WORKDIR/.opencode/skills"
    cp -rf /opt/agentteams/skills/. "$WORKDIR/.opencode/skills/"
fi

# 种子 opencode 全局配置（存在即不覆盖——排障时可挂 ConfigMap 手改）
CFG_DIR="$HOME/.config/opencode"
mkdir -p "$CFG_DIR/tools"
if [ ! -f "$CFG_DIR/opencode.json" ]; then
    cp /opt/agentteams/opencode.json "$CFG_DIR/opencode.json"
fi
cp /opt/agentteams/tools/bash.ts "$CFG_DIR/tools/bash.ts"

# bash 工具转发目标：默认同容器 helper 回环地址
export SANDBOX_EXEC_URL="${SANDBOX_EXEC_URL:-http://127.0.0.1:${HELPER_PORT}}"

python3 /opt/agentteams/sandbox_helper.py &
exec opencode serve --port "$OPENCODE_PORT" --hostname 0.0.0.0
```

要点：`opencode.json` **存在即不覆盖**——内网想换 provider/模型时挂
ConfigMap 到 `$HOME/.config/opencode/opencode.json` 即可，无需重建镜像
（见 §5.5）。

#### 1.3.3 opencode.json（仓库内是占位符）

```json
{
  "$schema": "https://opencode.ai/config.json",
  "model": "zhipu-coding/glm-5.3-flash",
  "autoupdate": false,
  "provider": {
    "zhipu-coding": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Zhipu GLM Coding Plan",
      "options": {
        "baseURL": "https://open.bigmodel.cn/api/coding/paas/v4",
        "apiKey": "__ZHIPU_API_KEY__"
      },
      "models": { "glm-5.3-flash": { "name": "GLM 5.3 Flash" } }
    }
  }
}
```

`__ZHIPU_API_KEY__` 在构建期由 `--build-arg ZHIPU_API_KEY=<key>` sed 替换。
**key 只出现在构建命令行，永不进 git**。

#### 1.3.4 tools/bash.ts（同名 custom tool 顶掉内置 bash）

opencode 里**同名 custom tool 优先于内置 tool**——这是把命令执行改道到
helper `/exec` 的机制（`worker-bridge/cimicode-runtime/tools/bash.ts:21`）：

```typescript
export default tool({
  description: "Execute shell commands in the agent execution environment …",
  args: {
    command: tool.schema.string().describe("The shell command to execute"),
    timeout: tool.schema.number().optional().describe("Timeout in seconds (max 900)"),
  },
  async execute(args) {
    const base = process.env.SANDBOX_EXEC_URL
    if (!base) return "sandbox exec unavailable: SANDBOX_EXEC_URL is not configured"
    const timeout = Math.min(Math.max(args.timeout ?? 600, 1), 900)
    const res = await fetch(`${base.replace(/\/$/, "")}/exec`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ command: args.command, timeout }),
      signal: AbortSignal.timeout((timeout + 15) * 1000),
    })
    …
    const data = (await res.json()) as { exitCode: number; stdout: string; stderr: string }
    let out = ""
    if (data.stdout) out += `${data.stdout}\n`
    if (data.stderr) out += `${data.stderr}\n`
    out += `[exit code: ${data.exitCode}]`
    return out
  },
})
```

#### 1.3.5 sandbox_helper.py 端点表（:4097，纯 stdlib）

| 端点 | 方法 | 用途 | 调用方 |
|---|---|---|---|
| `/healthz` | GET | 存活探针 | operator readiness（可选） |
| `/agents-md` | POST | body=markdown 原文 → **原子写** AGENTS.md 到 WORKDIR | bridge 每 turn |
| `/exec` | POST | `{"command","timeout"}` → `{exitCode,stdout,stderr}` | bash.ts 转发 |

### 1.4 worker-bridge-operator（Python）

单文件 operator（637 行，`worker-bridge/operator/worker_bridge_operator.py`），
10s 一轮 level-triggered 全量 reconcile（`RECONCILE_INTERVAL`），幂等收敛。
模块头注释即设计文档（:1-52），核心语义：

```
spec.runtime=worker-bridge 且 adapterMode 分派：
  cimicode-stateless → 零供给（bridge 直调外部平台，绑定走 runtime.yaml bridge 段）
  cimicode-pod       → 供给三件套 + 回写 env 三键：
        Service     <w>-cimicode-svc   (runtime :4096 / helper :4097)
        Deployment  <w>-cimicode
        Secret      <w>-cimicode-fs    (MinIO 凭据，从 bridge pod env 明文复制)
        Worker spec.env += BRIDGE_RUNTIME_ADAPTER=cimicode-pod
                           BRIDGE_RUNTIME_BASE_URL=http://<w>-cimicode-svc.<ns>.svc:4096
                           BRIDGE_RUNTIME_HELPER_URL=…:4097
删除 worker / 切 runtime / 切 stateless → managed-by label GC 三件套。
```

#### 1.4.1 reconcile 主流程（`worker_bridge_operator.py:541`）

```python
# worker_bridge_operator.py:541（节选）
def reconcile_worker(self, worker: str, worker_obj: dict[str, Any]) -> None:
    mode = self.adapter_mode(worker_obj)          # 空 → 规范化为 cimicode-pod
    if mode == ADAPTER_STATELESS:
        return                                    # 零供给
    if not self.cfg.cimicode_image:
        log.error("worker %s: CIMICODE_IMAGE is not set — provisioning deferred …", worker)
        return                                    # fail-loud：每轮报错，绝不跑错镜像
    # bridge pod env：MinIO 凭据的权威来源（controller 组装的明文 env）。
    # pod 未起（双候选都 404）→ 本轮推迟，下一轮重试——凭据到位前建
    # Secret/Deployment 只会得到一个永远连不上 FS 的 pod。
    bridge_env = self.bridge_pod_env(worker)
    if bridge_env is None:
        log.info("worker %s: bridge pod not found yet — provisioning deferred to next pass", worker)
        return
    creds = self.fs_credentials(bridge_env)
    with_fs_secret = False
    if self.cfg.fs_endpoint:
        if not creds:
            log.warning("worker %s: bridge pod env carries no %s/%s — provisioning deferred …",
                        worker, FS_ACCESS_KEY_ENV, FS_SECRET_KEY_ENV)
            return
        self.ensure_secret(self.cimicode_secret_name(worker), worker, creds)
        with_fs_secret = True
    else:
        log.warning("worker %s: AGENTTEAMS_FS_ENDPOINT not set — degraded plain-chat pod …", worker)
    team = self.team_for(worker)                  # Team CR workerMembers 反查
    matrix_user = self.matrix_user_id(worker, worker_obj)   # status.matrixUserID 优先
    self.ensure_service(self.svc(worker))
    self.ensure_deployment(self.cimicode_deployment(
        worker, team=team, matrix_user=matrix_user, with_fs_secret=with_fs_secret))
    self.ensure_worker_env(worker, worker_obj)    # 回写三键
```

**为什么凭据要等 bridge pod**：controller 把 MinIO 明文凭据组装进 bridge pod
env（worker_env.go），operator 是"消费者"——从 bridge pod env 读出来复制进
Secret，cimicode pod 再以 secretKeyRef 引用。bridge 未起说明 worker 还在供给中，
本轮推迟是正确行为（供给时序：controller 建 bridge pod → operator 下一轮读到
env → 建 cimicode 栈）。

#### 1.4.2 bridge pod 双候选名（`worker_bridge_operator.py:204`）

controller 的 bridge pod 命名有过一次演进（`agentteams-worker-<w>` →
`agentteams-worker-<w>-bridge`），operator 两个都探：

```python
# worker_bridge_operator.py:204（节选）
BRIDGE_POD_PREFIX = "agentteams-worker-"
BRIDGE_POD_SUFFIX = "-bridge"

def bridge_pod_env(self, worker: str) -> dict[str, str] | None:
    for name in (
        f"{BRIDGE_POD_PREFIX}{worker}{BRIDGE_POD_SUFFIX}",
        f"{BRIDGE_POD_PREFIX}{worker}",
    ):
        try:
            pod = self.core.read_namespaced_pod(name, self.cfg.namespace)
        except ApiException as exc:
            if exc.status != 404:
                raise
            continue
        envs = {e.name: e.value for c in pod.spec.containers for e in c.env or [] if e.value is not None}
        return envs
    return None    # 都不存在 = bridge 未起
```

#### 1.4.3 cimicode Deployment 的 env 组装（`worker_bridge_operator.py:276`）

```python
# worker_bridge_operator.py:288（节选）
# FS_* 契约同 worker-bridge/cimicode-sandbox：绝不设置 AGENTTEAMS_RUNTIME——
# mc 同步只认 ("k8s","aliyun")，其余走 local 静态三元组模式。
pod_env: dict[str, str] = {
    "AGENTTEAMS_FS_ROOT": "/workspace",
    "AGENTTEAMS_WORKER_NAME": worker,
    "SANDBOX_EXEC_URL": f"http://127.0.0.1:{self.cfg.cimicode_helper_port}",
    "OPENCODE_PORT": str(self.cfg.cimicode_port),
}
if self.cfg.fs_endpoint:
    pod_env["AGENTTEAMS_FS_ENDPOINT"] = self.cfg.fs_endpoint
    pod_env["AGENTTEAMS_FS_BUCKET"] = self.cfg.fs_bucket
if team:
    pod_env["AGENTTEAMS_TEAM"] = team
if matrix_user:
    pod_env["AGENTTEAMS_MATRIX_USER_ID"] = matrix_user
…
if with_fs_secret:      # 凭据不经明文 env：secretKeyRef 引用 <w>-cimicode-fs
    container_env += [client.V1EnvVar(name=FS_ACCESS_KEY_ENV, value_from=…), …]
```

**`AGENTTEAMS_TEAM` / `AGENTTEAMS_MATRIX_USER_ID` 为什么重要**：pod 内没有
agt CLI，taskflow 的团队路径解析与 `--actor` 全靠这两个 env；TEAM 由
`team_for()` 反查 Team CR workerMembers 得到（`worker_bridge_operator.py:179`）。

卷与探针（同函数）：

```python
# emptyDir：会话/工作区容器重启可存活；pod 重建丢失由 bridge adapter 的
# 404 自愈 + 每 turn 重推 AGENTS.md 兜住。
volumes=[client.V1Volume(name="workspace", empty_dir={})],
…
container.readiness_probe = client.V1Probe(
    http_get=client.V1HTTPGetAction(
        path=self.cfg.cimicode_probe_path,   # 默认 GET /session（镜像契约钉死）
        port=self.cfg.cimicode_port),
    initial_delay_seconds=5, period_seconds=10)
```

#### 1.4.4 env 三键回写（`worker_bridge_operator.py:474`）

```python
# worker_bridge_operator.py:474
def ensure_worker_env(self, worker: str, worker_obj: dict[str, Any]) -> None:
    svc_dns = self.cfg.cluster_dns(self.cimicode_svc_name(worker))
    wanted = {
        ENV_KEY_ADAPTER: ADAPTER_POD,
        ENV_KEY_BASE_URL: f"http://{svc_dns}:{self.cfg.cimicode_port}",
        ENV_KEY_HELPER_URL: f"http://{svc_dns}:{self.cfg.cimicode_helper_port}",
    }
    current = self.worker_spec_env(worker_obj)
    if all(current.get(k) == v for k, v in wanted.items()):
        return
    merged = dict(current)
    merged.update(wanted)
    body = {"spec": {"env": merged}}
    self.custom.patch_namespaced_custom_object(
        GROUP, VERSION, self.cfg.namespace, WORKERS_PLURAL, worker, body)
```

注意：patch 的是 **Worker CR spec.env**——controller 不会因此滚动 bridge pod
（5 个绑定字段已从 hash 排除，见 §1.5.3），bridge 由 15s 自愈轮询热接。

#### 1.4.5 deployment_drift（含 781e149d 修复点，`worker_bridge_operator.py:408`）

比较维度：image、readiness path、明文 env map、secretKeyRef 引用集合、卷
**类型存在性**、node_selector。卷比较的注释就是那次修复（见 §2.3）。

#### 1.4.6 operator env 总表（OperatorConfig，`worker_bridge_operator.py:101`）

| env | 默认 | 说明 |
|---|---|---|
| `WATCH_NAMESPACE` | （必填） | 缺失直接退出（main :625） |
| `CIMICODE_IMAGE` | （必填，无默认） | cimicode-runtime 镜像；缺失每轮 error log 不供给 |
| `CIMICODE_PORT` / `CIMICODE_HELPER_PORT` | 4096 / 4097 | 镜像契约钉死的端口对 |
| `CIMICODE_PROBE_PATH` | `/session` | readiness 路径；空串禁用 |
| `AGENTTEAMS_FS_ENDPOINT` / `AGENTTEAMS_FS_BUCKET` | 空 / `agentteams-storage` | MinIO 端点/桶；缺失降级纯聊天 pod |
| `MATRIX_SERVER_NAME` | 空 | status.matrixUserID 空时的 MXID 兜底域 |
| `RECONCILE_INTERVAL` | 10 | 轮询间隔秒 |
| `DEFAULT_RUNTIME` | 空 | spec.runtime 为空时的生效值（默认只认显式 worker-bridge） |
| `PROVISION_NODE_SELECTOR` | 空 | 钉节点（kubernetes.io/hostname） |

### 1.5 agentteams-controller（Go）

#### 1.5.1 新 CRD 字段与 runtime enum（5ea2e100）

`api/v1beta1`（`agentteams-controller/api/v1beta1/types.go`，CRD 的
schema 同步在 `config/crd/` 与 `helm/agentteams/crds/` **双处**，改一处必须
同步另一处）：

- `WorkerSpec.Runtime` enum 加 `worker-bridge`
- `WorkerSpec` 新增 5 个绑定字段：`AdapterMode`（enum：
  `cimicode-stateless | cimicode-pod`）、`CimicodeGatewayUrl`、`SessionId`、
  `SandboxId`、`TemplateId`
- HTTP API 三结构（create/update/response）同步流转：update 非空覆盖、空不动；
  runtime 原样落 CR，不在 create 期解析默认值

#### 1.5.2 runtime.yaml 顶层 bridge 段投影（runtime_config.go）

**这是 Worker CR 与 bridge pod 之间唯一的声明式绑定通道**：

```go
// agentteams-controller/internal/service/runtime_config.go:71
// memberRuntimeConfigBridge is the top-level "bridge" section projected for
// runtime=worker-bridge workers — the sole binding channel between the
// Worker CR and the bridge pod (consumed by the bridge's
// runtime_bridge_section parsing; see bootstrap.WorkerBootstrapConfig).
type memberRuntimeConfigBridge struct {
    AdapterMode string `json:"adapterMode,omitempty"`
    BaseURL     string `json:"baseUrl,omitempty"`
    SessionID   string `json:"sessionId,omitempty"`
    SandboxID   string `json:"sandboxId,omitempty"`
    TemplateID  string `json:"templateId,omitempty"`
}
```

投影条件与规范化（`runtime_config.go:380`）——**无绑定不投影**（bridge 保持
未定态等 env，这正是 pod 模式由 operator 接线的路径）：

```go
// runtime_config.go:380
if runtime == backend.RuntimeWorkerBridge && workerBridgeHasBinding(req.Spec) {
    adapterMode := strings.TrimSpace(req.Spec.AdapterMode)
    if adapterMode == "" {
        adapterMode = "cimicode-pod"     // 空 → 规范化 cimicode-pod
    }
    doc.Bridge = &memberRuntimeConfigBridge{
        AdapterMode: adapterMode,
        BaseURL:     strings.TrimSpace(req.Spec.CimicodeGatewayUrl),
        SessionID:   strings.TrimSpace(req.Spec.SessionId),
        SandboxID:   strings.TrimSpace(req.Spec.SandboxId),
        TemplateID:  strings.TrimSpace(req.Spec.TemplateId),
    }
}
```

```go
// runtime_config.go:432
// workerBridgeHasBinding reports whether the WorkerSpec carries any
// worker-bridge binding field. When false, no bridge section is projected at
// all — the bridge stays undetermined and waits for its own env-based
// resolution (e.g. an operator-provisioned runtime pod).
func workerBridgeHasBinding(spec v1beta1.WorkerSpec) bool {
    return strings.TrimSpace(spec.AdapterMode) != "" ||
        strings.TrimSpace(spec.CimicodeGatewayUrl) != "" ||
        strings.TrimSpace(spec.SessionId) != "" ||
        strings.TrimSpace(spec.SandboxId) != "" ||
        strings.TrimSpace(spec.TemplateId) != ""
}
```

落到 MinIO 的文件是 `agents/<w>/runtime/runtime.yaml`，YAML 形如：

```yaml
bridge:
  adapterMode: cimicode-pod
matrix: {…}
member: {…}
```

#### 1.5.3 绑定变更不触发 pod 重建（worker_controller.go）

`hashAppliedWorkerSpec` 是"spec 变化是否要滚动 pod"的判据。5 个绑定字段被
显式排除（`worker_controller.go:827/:924`）——**绑定变更只重投影 runtime.yaml，
由运行中 bridge 自愈热接，绝不能重建 pod**：

```go
// agentteams-controller/internal/controller/worker_controller.go:919
// zeroWorkerBridgeBindingFields clears the worker-bridge binding fields from
// a spec before hashing: they are projected into the runtime.yaml bridge
// section (agents/<name>/runtime/runtime.yaml) and picked up by the running
// bridge pod through its self-heal polling — changing a binding must rebind
// the bridge, never rebuild the pod.
func zeroWorkerBridgeBindingFields(spec *v1beta1.WorkerSpec) {
    spec.AdapterMode = ""
    spec.CimicodeGatewayUrl = ""
    spec.SessionId = ""
    spec.SandboxId = ""
    spec.TemplateId = ""
}
```

两个 hash 变体（`hashAppliedWorkerSpec` :827 与
`hashAppliedWorkerSpecForRuntimeAndResources` :875）都调用了它。

#### 1.5.4 bridge pod 命名与镜像 fail-fast（kubernetes.go）

```go
// agentteams-controller/internal/backend/kubernetes.go:606
// bridgePodSuffix is appended to derived pod names for the worker-bridge
// runtime so the masquerade bridge pod is recognizable on sight
// (`agentteams-worker-<name>-bridge`). Explicit ContainerName requests
// (managers) bypass the suffix.
const bridgePodSuffix = "-bridge"

// kubernetes.go:615
// workerPodNames lists the pod names a worker's pod may live under, in
// lookup order: the plain prefixed name (agent runtimes), then the
// worker-bridge runtime name (created with the "-bridge" suffix).
func (k *K8sBackend) workerPodNames(name string) []string {
    plain := k.workerPodName(name)
    return []string{plain, plain + bridgePodSuffix}
}
```

Status / Delete / Start 全部经 `workerPodNames` 双名级联（plain → -bridge）；
Create 的冲突检查也覆盖两个候选名——防换 runtime 后旧名 pod 滞留双活。

镜像三段式解析与 fail-fast（`kubernetes.go:289`）：

```go
// kubernetes.go:289
case req.Runtime == RuntimeWorkerBridge && k.config.WorkerBridgeImage != "":
    image = k.config.WorkerBridgeImage
case req.Runtime == RuntimeWorkerBridge:
    // Fail fast instead of falling through to the generic WorkerImage:
    // a bridge pod silently running an openclaw worker image wedges at
    // bootstrap with no actionable signal.
    return nil, fmt.Errorf("no image for worker-bridge runtime: set spec.image or AGENTTEAMS_WORKER_BRIDGE_IMAGE")
```

#### 1.5.5 上游修复搬运与 registerAdmin（5ea2e100 / ba367f11）

- `5ea2e100` 同时搬入了官方 dev 分支的通用修复（matrix/synapse 账号重激活、
  oss sdk_admin、provisioner、agent-pod-template、element-web/postgres/
  synapse/minio 模板同步）——这些改动不特定于 worker-bridge，评审时可按
  "上游同步"看待。
- `ba367f11`：cluster init 的 registerAdmin 单发失败被记 non-fatal、admin
  账号要等手动重启 controller。根因是新装集群 synapse 接受了建号 PUT 但
  login 路径仍在预热。修法是套用兄弟步骤（waitForOSS/waitForMatrix）同款
  3s 间隔 / 5min 上限 retry 包络（ProvisionUser 本身幂等，
  register-or-login）。

### 1.6 helm chart

三处改动构成"镜像供数链"（`bfd04647`）：

```yaml
# helm/agentteams/values.yaml:368
    # worker-bridge (cimicode-bridge) image — deliberately empty by default:
    # fill per environment (internal registry); worker-bridge Create fails
    # fast unless spec.image or this env names an image, so an unset value
    # never silently runs an openclaw worker image as a bridge pod.
    workerBridge:
      repository: ""          # e.g. <internal-registry>/agentteams/cimicode-bridge
      tag: ""                 # defaults to global.imageTag when repository is set
```

```yaml
# helm/agentteams/templates/_helpers.tpl:203
{{/* worker-bridge (cimicode-bridge) image; renders "" when no repository is
     configured — the controller then fails fast on worker-bridge creates
     unless spec.image names an image (prevents silent openclaw fallback). */}}
{{- define "agentteams.worker.workerBridgeImage" -}}
{{- if .Values.worker.defaultImage.workerBridge.repository }}
{{- $tag := default (include "agentteams.globalImageTag" .) .Values.worker.defaultImage.workerBridge.tag }}
{{- printf "%s:%s" .Values.worker.defaultImage.workerBridge.repository $tag }}
{{- end }}
{{- end }}
```

```yaml
# helm/agentteams/templates/controller/deployment.yaml:91
            - name: AGENTTEAMS_WORKER_BRIDGE_IMAGE
              value: {{ include "agentteams.worker.workerBridgeImage" . | quote }}
```

`helm/agentteams/crds/` 与 `agentteams-controller/config/crd/` 同步了
runtime enum + 5 绑定字段（有 `make check-crd-sync` 校验）。

### 1.7 CLI / 模板 / 契约文档

**`worker-bridge/cli/`**（同时部署进 cimicode-runtime 镜像 PATH 与 leader 镜像）：

| 命令 | 侧 | 关键子命令/参数 |
|---|---|---|
| `taskflow` | worker | `check`（收件箱）、`ack <task>`、`submit <task> --deliverables <f1> <f2> …`、`inbox/list` |
| `agentteams-sync` | 双侧 | mc 三模式 alias 同步（`pull`/`push`/`list`），吃 `AGENTTEAMS_FS_*` 静态三元组 |
| `projectflow` | leader | 项目立项 / `delegate_task`（建任务）/ `check_task` / `plan_dag` / `complete_project` |

**已知 CLI 陷阱**（105 两轮实测踩中）：`taskflow submit --deliverables` 是
`nargs="*"`，**重复传 flag 只留最后一个**——必须在一条 `--deliverables`
参数里传全。规避方式：worker soul 里写明"deliverables 在一条参数里传全"
（t-i1 无提示踩坑、t-i2 有提示未踩，对照实验成立）。

**`worker-bridge/template/`**：两套 AGENTS.md 源模板（worker /
leader-agent），占位符只有 `{{COORDINATION}}`（组织视图：成员名单/角色）与
`{{ENVIRONMENT}}`（环境约束）两个；skills 树（task-management /
file-sharing / …）随模板进镜像。**模板不许写测试环境实现细节**（环境约束
在部署层解决）。

**`worker-bridge/contract/`**：
- `interface-contract.md` v2.4——协作接口（任务协议、文件流、验收语义）
- `controller-handover.md` v2.4——controller 供给语义交接
- `adapter-contract.md`——两形态传输契约/端口/接线三键/已知限制（对接新
  运行时前必读）

---

## 二、七个 bug 修复：before/after 代码对比

### 2.1 progress 历史回放（`f949adae`，bridge）

**现象**（105 实测）：worker 每 turn 回复携带全部历史叙述，bridge log
`progress=0,1,2,3...` 单调增长；wb-dev 单 turn 出现 12 条重复叙述。

**根因**：`_completed_assistant_texts` 只按 **id 排除 baseline 那一条**——
baseline **之前**的历史 assistant 回复也是已完成消息，被全量收进本 turn 的
progress_texts。

**before → after**（真实 diff，`git show f949adae`）：

```diff
     @staticmethod
+    def _slice_after_baseline(
+        messages: list[dict[str, Any]], baseline_id: str
+    ) -> list[dict[str, Any]] | None:
+        """baseline 之后（按列表位置）的消息切片；baseline_id 空返回全表。
+
+        返回 None 表示 baseline_id 非空却不在列表中（session 状态异常）。
+        消息列表按时间序——只按 id 排除单条会让历史回复混进本 turn 的
+        扫描窗口（progress 回放 / 轮询捞到旧回复），必须按位置截断。
+        """
+        if not baseline_id:
+            return messages
+        for idx, message in enumerate(messages):
+            if str((message.get("info") or message).get("id") or "") == baseline_id:
+                return messages[idx + 1:]
+        return None
+
     @staticmethod
     def _completed_assistant_texts(messages, baseline_id):
+        if baseline_id:
+            messages = CimicodePodAdapter._slice_after_baseline(messages, baseline_id) or []
         texts: list[str] = []
         for message in messages:
             …
-            if not message_id or message_id == baseline_id:   # 只排单条 id ❌
+            if not message_id:                                 # 位置截断后无需排 id ✅
```

`_poll_reply` 轮询窗口同接入（f949adae diff 的第二段）：

```diff
         while True:
             await asyncio.sleep(self.poll_interval_seconds)
-            messages = await self._messages(session_id)
-            for message in reversed(messages):
+            fresh = self._slice_after_baseline(await self._messages(session_id), baseline_id)
+            if fresh is None:
+                # baseline 非空却不在列表里（session 状态异常）——本轮跳过，
+                # 继续等 deadline 兜底；绝不扫全表（会把历史回复当新完成）。
+                continue
+            for message in reversed(fresh):
```

**验证**：回归测试 `test_progress_does_not_replay_history`；镜像 v0.3.4-wb
实测 progress 恒为本 turn 真实计数（第五~七轮 15+ worker-turn 复验，长工具
turn progress=7/9/15 均为单 turn 计数）。

### 2.2 Failed Team 重试链死亡（`da22acf4`，controller）★重点

**现象**（105 实测，t-i1/t-i2 两次触发）：建 team 撞凭据竞态（team
reconciler 先于 worker 凭据 Secret 跑存储刷新 → "credentials not found"）
后，Team 卡 Failed **3 分 20 秒**无任何重试 pass，需人工 annotate 唤醒。

**根因（日志级还原）**：两次快速 failTeam（第二次常读 informer 旧对象直接
绕过退避 guard）在等待队列**按对象 key 去重**合并为一次唤醒（第二次
AddAfter 是 no-op）；唯一唤醒按**第一次**失败时间 +30s 触发，guard 却拿
**第二次**失败的 PhaseTransitionTime 比较（差 3.4s < 30s）→ 裸 return 丢弃
且不排新唤醒 → 重试链永久死亡。

**before → after**（真实 diff，`git show da22acf4`，team_controller.go；
human_controller.go 同型修复）：

```diff
     if team.Status.Phase == "Failed" && !team.Status.MaxRetriesReached &&
         team.Status.ConsecutiveFailures > 0 && team.Status.PhaseTransitionTime != nil {
-        if time.Since(team.Status.PhaseTransitionTime.Time) < failBackoffFor(team.Status.ConsecutiveFailures) {
-            return reconcile.Result{}, nil          // 裸 return：信任 failTeam 已排的唤醒 ❌
+        if elapsed := time.Since(team.Status.PhaseTransitionTime.Time); elapsed < failBackoffFor(team.Status.ConsecutiveFailures) {
+            remaining := failBackoffFor(team.Status.ConsecutiveFailures) - elapsed
+            return reconcile.Result{RequeueAfter: remaining}, nil   // 重排剩余窗口 ✅
         }
     }
```

修复后的完整注释（team_controller.go:210-226）把"为什么裸 return 会死链"
讲透了——唤醒链不再依赖可能已被消费的定时器。

**并发安全**（为什么不会放大重试频率）：workqueue 对同一对象 key 互斥；
item 已在等待队列时 AddAfter 是 no-op（任意时刻同 Team 最多 1 个待触发
唤醒）；重试边界不变（30s 起步指数退避 30s/1m/2m/4m/8m cap 10min、5 次
封顶 maxRetriesReached 后停等人工）。

**验证**：4 个回归测试 + 105 实测——t-fix1 撞竞态 **39 秒自愈**（修复前
同场景 3m20s）；第七轮 3 team 一次性 apply 全撞竞态（t-p3 双重失败死亡
时序），38s/59s/46s 全部自愈，零人工。

**人工兜底**（maxRetriesReached 后重置重试预算）：

```bash
kubectl -n <ns> annotate team <name> agentteams.io/retry=$(date +%s) --overwrite
```

### 2.3 operator 幻影漂移（`781e149d`）

**现象**：operator 每轮都报 volume 漂移并反复空转 replace。

**根因**：desired 侧 `empty_dir={}` 是 falsy，API 回读侧物化为
`V1EmptyDirVolumeSource()` 实例是 truthy——`bool()` 比较永不相等。

**修法**（worker_bridge_operator.py:419 现状代码）：

```python
# `is not None` 而非 bool()：desired 侧 empty_dir 是 {}（falsy），API
# 回读侧物化为 V1EmptyDirVolumeSource 实例（truthy）——bool() 比较会
# 造成每 pass 幻影漂移（反复空转 replace）。
live_volumes = [
    (v.name, v.empty_dir is not None, v.host_path is not None)
    for v in live.spec.template.spec.volumes or []
]
```

即卷比较只比**卷类型存在性指纹**，不比对卷对象的真值。

### 2.4 mc 构建期下载下线（`63578ceb`）

dl.min.io 的 latest 式下载路径 410 Gone，且真实内网构建无外网。修法：从
已验证镜像提取 mc（RELEASE.2025-08-13T08-35-41Z，linux-amd64）vendor 进
`worker-bridge/cimicode-runtime/bin/mc`，Dockerfile `COPY … /usr/local/bin/mc`
直装。**换架构（arm64）构建时这个二进制要换**。

### 2.5 operator 镜像构建上下文（`193b4166`）

operator Dockerfile 的 `COPY worker_bridge_operator.py` 相对**构建上下文**
解析；仓库根上下文下该路径不存在。修法（Makefile 现状）：

```makefile
# Makefile:242
build-worker-bridge-operator: ## Build worker-bridge-operator image
	# context = operator 目录（Dockerfile 的 COPY 路径相对该目录）
	docker build $(PLATFORM_FLAG) $(DOCKER_BUILD_ARGS) \
		-f worker-bridge/operator/Dockerfile \
		-t $(LOCAL_WORKER_BRIDGE_OPERATOR) \
		worker-bridge/operator
```

**记忆口诀**：bridge / cimicode-runtime 用仓库根（COPY 带 `worker-bridge/`
前缀），operator / controller 用各自目录。

### 2.6 cluster init registerAdmin 竞态（`ba367f11`）

见 §1.5.5。修法本质：把单发调用套进 3s/5min retry 包络，对齐
waitForOSS/waitForMatrix 兄弟步骤。

### 2.7 minio service.type：加入后 revert（`d596b075` + `73abde92`）

让 values 的 `storage.minio.service.type` 真正生效（默认 ClusterIP 保留
headless `clusterIP: None`；NodePort/LB 时可钉 apiNodePort/consoleNodePort）。
在功能分支上先加入后被 revert（无原因记录），**净效果：功能分支无此改动**。
实现保留在测试分支 `test/worker-bridge-105`（5baabe4c）供 105 NodePort 暴露
minio console（31601）使用。**用户决策：不 cherry-pick 回 feature**。如将来
要回迁：cherry-pick 5baabe4c，默认行为逐字节不变。

---

## 三、放到内网需要注意什么（部署坑全集）

以下坑全部来自 105（k3s 单节点，内网无外网）真实部署验证。命令以
`<占位符>` 通用形式给出。

### 3.1 四个镜像的构建（Makefile，注意上下文两套相反）

| target | 构建上下文 | 特殊 build-arg |
|---|---|---|
| `make build-agentteams-controller` | `./agentteams-controller/` | Makefile 会先 `cp manager/agent → agentteams-controller/agent`；**还要 `--build-context shared=<repo>/shared/lib`**（SHARED_LIB_CTX） |
| `make build-cimicode-bridge` | 仓库根 | — |
| `make build-cimicode-runtime` | 仓库根 | **`ZHIPU_API_KEY=<key>` 必填**（缺则构建失败）；`NPM_REGISTRY=<内网 npm mirror>`；`OPENCODE_VERSION=1.18.27`（默认已钉） |
| `make build-worker-bridge-operator` | `worker-bridge/operator/` | — |

```bash
# 内网完整命令形（在仓库根）：
make build-agentteams-controller
make build-cimicode-bridge
make build-cimicode-runtime ZHIPU_API_KEY=<key> NPM_REGISTRY=https://<内网npm镜像>
make build-worker-bridge-operator
```

**必须知道的坑**：

1. **上下文相反**：`build-cimicode-bridge` / `build-cimicode-runtime` 的
   Dockerfile 里 COPY 路径带 `worker-bridge/` 前缀，上下文必须是仓库根；
   `build-worker-bridge-operator` / `build-agentteams-controller` 的上下文是
   各自目录。搞反了报 `COPY failed: file not found`。
2. **ZHIPU_API_KEY 只在命令行出现**：仓库内 opencode.json 是
   `__ZHIPU_API_KEY__` 占位符，构建期 sed 替换并校验替换完全。**key 永不
   进 git / helm values 之外的任何持久文件**。
3. **mc 不下载**：镜像内 vendored `bin/mc` 直装（dl.min.io 已 410）。
   arm64 环境需自行替换该二进制。
4. **ripgrep 必装**：node:22-slim 不带 rg，opencode 的 skill 工具每次调用
   会挂 130s 再报错（Dockerfile 注释有记载）。
5. **CRLF**：仓库根 `.gitattributes` 钉了 `*.sh eol=lf`。Windows 开发机克隆
   后如果被 IDE 改成 CRLF，entrypoint 的 shebang 会坏（镜像里已加 `sed -i
   "s|\r$||"` 双保险，但别依赖它）。
6. **go 构建**走 goproxy.cn（go.dev 不通）。

### 3.2 镜像进集群（无内部 registry 时的 k3s import）

```bash
docker save agentteams/cimicode-bridge:<tag> -o /tmp/bridge.tar
sudo ctr -n k8s.io images import /tmp/bridge.tar
```

**坑**：`docker save … | sudo ctr import -` 管道形式**首行会被 sudo 当密码
吃掉**——必须先落 tar 文件再 import。

### 3.3 helm 安装/升级

```bash
sudo -A env KUBECONFIG=/etc/rancher/k3s/k3s.yaml helm -n <ns> upgrade <release> \
    <repo>/helm/agentteams -f values-<env>.yaml
```

**坑**：
1. `sudo` 会剥 `KUBECONFIG` env——必须 `sudo -A env KUBECONFIG=… helm …`。
2. `sudo -S` 吃 stdin：任何要往远端喂 stdin 的操作（`kubectl apply -f -`、
   `exec -i < file`）全被密码提示吞掉。用 askpass 脚本解放 stdin：
   ```bash
   printf '#!/bin/sh\necho "<password>"\n' > /tmp/rap.sh && chmod +x /tmp/rap.sh
   # 之后所有 sudo 用：SUDO_ASKPASS=/tmp/rap.sh sudo -A <cmd>
   ```
   注意 sudo 的 `env_reset` 会清环境变量，所以 askpass 脚本里**必须写明文**
   （`echo "$VAR"` 形式的 env 传递会失败）；脚本权限 600，用完即删。
3. **higress chart 依赖**：`helm dependency update` 走不了外网时，把
   依赖 tgz 手工拷进仓库树 `charts/`。
4. values 必填段（缺了 worker-bridge Create 直接报错，这是故意的 fail-fast）：

```yaml
worker:
  defaultImage:
    workerBridge:
      repository: <internal-registry>/agentteams/cimicode-bridge
      tag: v0.3.4-wb            # 任意，但必须显式
```

### 3.4 operator 部署（deploy/operator.yaml 范本）

必填 env：`WATCH_NAMESPACE`、`CIMICODE_IMAGE`（cimicode-runtime 镜像全名）、
`AGENTTEAMS_FS_ENDPOINT`（MinIO svc 地址，如
`http://agentteams-<x>-minio.<ns>.svc:9000`）；可选
`PROVISION_NODE_SELECTOR`（单节点集群钉节点）、`AGENTTEAMS_FS_BUCKET`。

RBAC 需要：workers/teams CR 读写、pods 读、secrets 全套、deployments/
services 读写（范本已含）。

### 3.5 CR 与操作类坑（105 实测）

| # | 坑 | 说明/规避 |
|---|---|---|
| 1 | CR 必带 label | kubectl apply 的 Worker/Team 必须带 `agentteams.io/controller: <controller名>`（informer 按此过滤）；controller HTTP API 会强制盖，kubectl 直 apply 不会 |
| 2 | 先 Worker 后 Team | 建 team 前成员 worker 必须已存在；删除时先 Team 后 worker，串行等 finalizer |
| 3 | Team 凭据竞态 | team reconciler 可能先于 worker 凭据 Secret 跑 → Failed。v1.2.2.12-wb 起**自愈**（30s 级）；重试预算耗尽才需 `kubectl annotate team <t> agentteams.io/retry=$(date +%s) --overwrite` |
| 4 | Team 级 runtime config 所有权 | 成员 LLM 配置在 **Team 创建时**生成存 MinIO——改配置只重建 Worker 不刷新，必须连 Team 一起重建 |
| 5 | Matrix 消息格式 | 必带 `msgtype: "m.text"`；`m.mentions` 必须是对象 `{"user_ids":[MXID]}`（list 报 M_BAD_JSON）；**DM 也要求 @mention**（filter 的 is_group 恒 True） |
| 6 | m.file 无法带 mention | 文件事件 body 是文件名——先发文本 mention 指令、文件紧随；文件通道实际走 MinIO 共享（leader 中继实测全通） |
| 7 | mc 路径带 bucket 前缀 | mc alias 指向 endpoint 根，对象键要写全 `agentteams-storage/teams/<t>/shared/...` |
| 8 | minio svc headless | 不能原地 patch 成 NodePort（test 分支 5baabe4c 提供了 values 化方案：先删再 upgrade 重建） |
| 9 | 节点 pod 容量 | 单节点注意 kubelet maxPods（105 是 110）；每个 worker-bridge worker 吃 2 个 pod（bridge + cimicode） |
| 10 | 绝不给 cimicode pod 设 `AGENTTEAMS_RUNTIME` | mc 同步只认 k8s/aliyun 两值，误设会走错模式；本 pod 契约是 local 静态三元组（operator 组装的 env 已是正确形态，别手动加） |
| 11 | 后台 ssh 任务杀不掉 | 本地 TaskStop/断开不会终止远端 shell——apply 类操作在远端会继续跑；危险操作前确认远端无残留进程 |
| 12 | 容器日志时区 | pod 日志是 UTC，对时间线时本地 +8 |

### 3.6 dashboard 侧（**不在本分支**，部署时须知）

1. **worker-bridge UI 支持**目前是 105 测试资产 `dashboard-fork-patch3.py`
   的 fork 补丁（4 处 assert-then-replace：agentteams-api.ts 加
   `'worker-bridge'` 与 `adapterMode`、runtime-meta.ts 补条目、
   worker-create-dialog.tsx 运行模式下拉、workers-section.tsx 删
   opencode→omit 残留）。**合入主干前需转正进 dashboard 仓库**——本文档
   读者如负责 dashboard，这就是你的待办。
2. **dashboard→controller 认证 wiring**（部署层，不改代码）：admin SA
   （`<RESOURCE_PREFIX>admin`，controller 按前缀映射 admin 角色，无需
   RBAC）+ Deployment `serviceAccountName` + 投影 token volume（audience=
   `agentteams-controller`）+ env `AGENTTEAMS_AUTH_TOKEN_FILE`。缺这个
   wiring 时 dashboard 一切写操作 401。
3. dashboard 自带 AI 聊天的 `AGENTTEAMS_OPENAI_BASE_URL` 与 worker-bridge
   无关，需另行指向有效网关。

### 3.7 每步部署的校验命令（按序执行）

```bash
NS=<ns>
# ① controller 起来且镜像 env 就位
kubectl -n $NS get deploy -o yaml <controller> | grep AGENTTEAMS_WORKER_BRIDGE_IMAGE
# ② operator 起来
kubectl -n $NS logs deploy/worker-bridge-operator | head   # 应看到 starting ns=… cimicode=<image>
# ③ 建 Worker 后：bridge pod
kubectl -n $NS get pod -w | grep -E '<w>-bridge|<w>-cimicode'
# ④ operator 回写三键
kubectl -n $NS get worker <w> -o jsonpath='{.spec.env}' | python3 -m json.tool
# ⑤ bridge 自愈接上（日志锚点见 §1.2.1/§1.2.6）
kubectl -n $NS logs agentteams-worker-<w>-bridge | grep -E 'recovered|turn start|adapter'
# ⑥ cimicode 栈
kubectl -n $NS get deploy,svc,secret | grep '<w>-cimicode'
kubectl -n $NS exec <w>-cimicode-<rs>-<hash> -- curl -s localhost:4096/session
kubectl -n $NS exec <w>-cimicode-<rs>-<hash> -- curl -s localhost:4097/healthz
```

---

## 四、怎么用：从建 Worker 到跑完一个项目

### 4.1 建团队（CR，kubectl 路径）

完整可用样例（105 第七轮资产 `teams-round7.yaml` 的范式）：

```yaml
apiVersion: agentteams.io/v1beta1
kind: Worker
metadata:
  name: p1-lead
  namespace: <ns>
  labels:
    agentteams.io/controller: <controller名>     # 必带（§3.5 坑1）
spec:
  runtime: copaw                                  # leader 用 copaw
  workerName: p1-lead
  displayName: p1-lead
  model: glm-5.3-flash
  env:
    COPAW_TOOL_GUARD_ENABLED: "false"             # 测试环境建议禁用工具护栏
  identity: |
    你是团队 t-p1 的组长（copaw 运行时）。职责：把 Manager 下达的项目要求
    自行拆解为任务并分派给合适的成员（用 projectflow 的 delegate_task 创建
    任务），跟踪执行、验收产出、向 Manager 汇总。分工由你根据项目要求与
    成员构成自行决定。
  soul: |
    风格：简洁、面向行动。任务书写清交付物与验收标准；验收前独立复核证据
    （复跑测试、实际执行示例）；汇总时给出结论与证据。
---
apiVersion: agentteams.io/v1beta1
kind: Worker
metadata:
  name: p1-dev
  namespace: <ns>
  labels: { agentteams.io/controller: <controller名> }
spec:
  runtime: worker-bridge
  adapterMode: cimicode-pod          # 空=规范化 cimicode-pod；另一形态 cimicode-stateless
  workerName: p1-dev
  displayName: p1-dev
  model: native-config               # 用镜像内置 provider（opencode.json），不出 model 段
  soul: |
    你是 Python 工程师。收到任务先 taskflow ack；按任务书实现模块并附最小
    单测，产物写入任务工作区后 taskflow submit（deliverables 在一条
    --deliverables 参数里传全）。返工时先读反馈再改，改完重新 submit。
---
apiVersion: agentteams.io/v1beta1
kind: Team
metadata:
  name: t-p1
  namespace: <ns>
  labels: { agentteams.io/controller: <controller名> }
spec:
  displayName: 日期工具小队
  description: 日期时间处理工具集——解析/格式化/时长计算与边界处理，交付须含单测、README 与独立集成验证
  teamName: t-p1
  workerMembers:
    - { name: p1-lead, role: team_leader }
    - { name: p1-dev,  role: worker }
    - { name: p1-qa,   role: worker }
```

要点：
- **先 apply 全部 Worker，再 apply Team**；删除反序。
- worker 的 `model: native-config` 表示用镜像烘焙的智谱 provider，controller
  不会在 runtime.yaml 出 model 段。
- soul 里写协议性提示（taskflow 流程、deliverables 单 flag）能有效规避已知
  CLI 陷阱（§1.7）。

HTTP API 路径（dashboard / 脚本用）：

```bash
TOKEN=$(kubectl -n $NS create token <RESOURCE_PREFIX>admin --audience=agentteams-controller)
curl -X POST "http://<controller-svc>:8090/api/v1/workers" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"wb-api1","displayName":"wb-api1","runtime":"worker-bridge",
       "adapterMode":"cimicode-pod","model":"native-config","soul":"…"}'
# 期望 201；响应回显 adapterMode
```

### 4.2 发消息（Manager → team room）

room ID 从 Team status 拿：`kubectl -n $NS get team t-p1 -o
jsonpath='{.status.teamRoomID}'`；worker MXID 从
`kubectl get worker p1-dev -o jsonpath='{.status.matrixUserID}'`。

```bash
HS=http://<synapse地址>       # 集群外经 NodePort，集群内用 svc
TOKEN=<admin access token>    # admin 账号的 Matrix access token

TXN=txn-$(date +%s)
curl -X PUT "$HS/_matrix/client/v3/rooms/$ROOM/send/m.room.message/$TXN" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
        "msgtype": "m.text",
        "body": "@p1-dev 请开始处理你收到的任务",
        "m.mentions": {"user_ids": ["@p1-dev:<domain>"]}
      }'
```

**格式硬约束**：`msgtype` 必填；`m.mentions` 必须是对象不是数组；群里不带
mention 的消息 worker 一律忽略（日志 `reason=not_mentioned`）——这是特性
不是 bug（防抢话）。

查房间消息（排障）：

```bash
curl "$HS/_matrix/client/v3/rooms/$ROOM/messages?dir=b&limit=20" \
  -H "Authorization: Bearer $TOKEN"
```

### 4.3 跑项目的标准剧本（105 七轮沉淀的协议）

1. **leader 介绍核验**：Manager 在群里 `@<leader>` 请其介绍团队构成——
   校验组织视图（coordination 段）正确再派活。
2. **任务书只提项目要求、不指定分工**：发给 `@<leader>`，例如
   "为本团队立项：日期时间处理工具集，交付须含单测、README 与独立集成验证"。
3. leader 自主 plan_dag → delegate_task 派发；worker 群内收到 mention，
   taskflow ack → 实现 → `taskflow submit --deliverables <全部文件一条传>`。
4. QA worker 独立复跑 + 只读哈希基线冻结；leader check_task 验收 →
   `complete_project` → 群内总结报告。
5. **MinIO 独立核验交付物**：

```bash
kubectl -n $NS exec <任意带mc的pod> -- \
  mc ls --recursive local/agentteams-storage/teams/t-p1/shared/
# 期望看到 tasks/<project>-*/（meta.json + 交付物）与 projects/<project>-*/
# 的 meta/plan/result 三件套
```

### 4.4 故障处理速查

| 症状 | 判定 | 处置 |
|---|---|---|
| Team 卡 Failed 不重试 | `kubectl get team <t> -o jsonpath='{.status}'` 看 maxRetriesReached | 未达上限等自愈（30s 级）；已达上限 `kubectl annotate team <t> agentteams.io/retry=$(date +%s) --overwrite` |
| worker 不回话 | bridge 日志搜 `reason=not_mentioned` / `no reply` / `Gateway turn failed` | mention 格式错误→改消息；turn failed→看 cimicode pod |
| cimicode pod 被杀/漂移 | bridge 日志 `session … vanished; recreating` | **无需人工**：Deployment 自动重建，adapter 404 自愈重建会话，每 turn 重推 agent.md；中断的 turn 用一条 nudge 消息重放 |
| bridge 长时间 Pending | 节点 pod 满（maxPods） | 腾节点；起 pod 后 initial sync 自动补消费挂机期间的消息（不丢） |
| operator 不供给 | operator 日志搜 `CIMICODE_IMAGE is not set` / `bridge pod not found yet` | 前者补 operator env；后者是正常时序（等下一轮） |
| 换了 adapterMode 不生效 | Worker spec.env 三键是否更新 | operator 10s 内回写；bridge 15s 自愈热接——都无需重启 pod |

### 4.5 日志/状态观测点汇总

```bash
# bridge 主链路（§1.2.1 锚点表）
kubectl -n $NS logs agentteams-worker-<w>-bridge --tail=200
# adapter 动作（§1.2.6 锚点表）
kubectl -n $NS logs agentteams-worker-<w>-bridge | grep 'cimicode-pod'
# cimicode pod（skills 发布 + helper listening + opencode 日志）
kubectl -n $NS logs <w>-cimicode-<rs>-<hash> --tail=100
# bridge 探针
kubectl -n $NS exec agentteams-worker-<w>-bridge -- curl -s localhost:8081/status
# agent.md 留档（每 turn 渲染结果）
mc cat local/agentteams-storage/agents/<w>/agent-md/latest.md | head -40
```

---

## 五、怎么改：常见扩展场景的操作手册

### 5.1 加一个新 adapter（如对接另一种运行时）

改 4 处 + 测试（参考 `43d115c3` 的提交内容）：

1. **新建** `worker-bridge/bridge/src/cimicode_bridge/runtime/<x>_adapter.py`：
   实现 `name` 类属性、`capabilities() -> RuntimeCapabilities`（契约类型在
   `runtime/base.py`）、
   `async chat(*, session_id, sandbox_id, turn_id, agent_md, history,
   user_message) -> list[RuntimeEvent]`（事件类型在 `events.py`；返回事件
   至少含一个终结事件：TURN_COMPLETED / RUNTIME_ERROR /
   TURN_INTERRUPTED）、`async health()`、`async close()`。
2. **注册** `runtime/registry.py`：`VALID_ADAPTERS` 加名字 + `build_runtime_
   adapter` 加分支。
3. **配置**（仅当需要新接线键）：`config.py` `RuntimeConfig` 加字段；
   `app.py _apply_env_overrides()` 加 env 拾取；`_recover_late_runtime_wiring`
   的 runtimeEnv 拾取段同步。
4. **门禁**：`app.py` 中三处 `== "cimicode-stateless"` 的 session 门禁语义
   是"仅 stateless 要求预建绑定"——新 adapter 若也无预建绑定，把判断改成
   `in {"cimicode-stateless"}` 集合形式（不要复制粘贴第三处）。
5. **测试**：`tests/unit/` 加假服务回环测试（参照
   `test_cimicode_pod_adapter.py` 的假 opencode+helper 模式：happy path /
   失败事件 / 超时 / 接线缺失 fail-loud / 会话自愈）。

### 5.2 改端口（4096/4097 → 别的）

端口是 operator 与镜像的**契约**，两处联动：

1. **镜像侧已参数化**：entrypoint.sh 读 `OPENCODE_PORT` /
   `BRIDGE_SANDBOX_HELPER_PORT`，bash.ts 读 `SANDBOX_EXEC_URL`——不用改镜像。
2. **operator 侧**：`CIMICODE_PORT` / `CIMICODE_HELPER_PORT` env 改值即可
   （svc 端口、Deployment env、SANDBOX_EXEC_URL、readiness、回写三键全由
   这两个值推导）。
3. bridge 侧无需改（base_url/helper_url 带端口由 operator 注入）。

即：**只改 operator Deployment 的两个 env**。若要改镜像内默认值再动
Dockerfile 的 `ENV` 行。

### 5.3 给 Worker CR 加字段（并投影到 runtime.yaml）

以加一个 `bridge.foo` 绑定字段为例（对齐 5ea2e100 的做法）：

1. `agentteams-controller/api/v1beta1/*types*.go`：WorkerSpec 加字段 + json tag。
2. **CRD 双处同步**：`config/crd/` 与 `helm/agentteams/crds/`（跑
   `make generate sync-crds check-crd-sync`）。
3. HTTP API 三结构（internal/server/types.go 的 create/update/response）流转。
4. 投影：`runtime_config.go` `memberRuntimeConfigBridge` 加字段 +
   `workerBridgeHasBinding` 视语义决定是否纳入 + 投影段赋值。
5. **若属绑定类**（改了不该重建 pod）：`worker_controller.go
   zeroWorkerBridgeBindingFields` 加一行清零。
6. bridge 消费侧：`bootstrap.py` 的 bridge 段解析 + `config.py` 字段 +
   adapter 使用。
7. 测试：controller 侧 deployer/projection 用例；bridge 侧 bootstrap 用例。

### 5.4 改 skills / 模板

- 模板与 skills 源码在 `worker-bridge/template/worker-bridge-agent/`；
  **改完必须重建 cimicode-runtime 镜像**（entrypoint 每次启动从镜像内副本
  重同步到 `$WORKDIR/.opencode/skills/`，emptyDir 里的旧副本会被覆盖）。
- 模板纪律：AGENTS.md 模板只允许 `{{COORDINATION}}` / `{{ENVIRONMENT}}`
  占位符；**不许写测试环境实现细节**。
- bridge 侧的 agent.md 渲染链：`app.py → prompt.py → generate_agent_md.py`，
  改段落结构动 generator（有 golden 测试护住）。

### 5.5 换 LLM provider / 模型（不改镜像的方式）

entrypoint 的种子逻辑是**存在即不覆盖**：把自定义 opencode.json 挂载到
`$HOME/.config/opencode/opencode.json`（ConfigMap + volume 挂载）即可覆盖
烘焙的智谱配置。要彻底换掉烘焙值则改 `cimicode-runtime/opencode.json` +
重建镜像（自有 provider 的 key 同样走 build-arg sed 占位符模式，别写进
仓库）。

### 5.6 会话持久化（emptyDir → PVC）

`worker_bridge_operator.py` `cimicode_deployment()` 的 volumes 段（:349）
把 `empty_dir={}` 换成 `persistent_volume_claim`，并在
`deployment_drift` 的卷指纹（:422-429）里把 PVC 存在性纳入比较（现在的
指纹已是 `(name, empty_dir is not None, host_path is not None)` 三元组，
加第四元 `pvc is not None`）。注意 PVC 是 per-worker 的，删 worker 时 GC
不回收 PVC（operator 只 GC Deployment/svc/Secret）。短期不做的理由：
adapter 404 自愈 + 每 turn 重推 agent.md + taskflow mc pull 已把丢失成本
压到一次 turn 重放（记 contract/adapter-contract.md known limitations）。

---

## 六、测试基线与分支状态

### 6.1 单测/构建（feature @ 1418812e）

| 套件 | 结果 |
|---|---|
| controller `go build ./...` / `go test ./...` | 通过（oss 包 2 个预存在 Windows 环境失败除外：temp 生成的 mc 脚本 Windows 不可执行，与改动无关，git stash 验证过） |
| bridge pytest（tests/ + tests/unit/） | 115 passed |
| operator pytest | 19 passed |

### 6.2 105 真实环境七轮实测（cimicode-test ns，真实 LLM）

| 轮 | 主题 | 结论 |
|---|---|---|
| 1 | API 建 worker / 五轮上下文 / 边界输入 / 并发 / 附件 | ✓（附件 DM 是已知 gap） |
| 2 | csv-utils 全流程 + 容错链（坏 meta.json → fail-loud → leader 绕行恢复） | ✓ |
| 3 | dashboard 全流程（认证 wiring 修复 + 建栈 + 任务看板） | ✓ |
| 4 | 扩编/并行/追问/返工 | 扩编组织视图不刷新=copaw 侧已知限制；返工教科书级闭环 |
| 5 | 新协议（护栏禁用/独立 team/介绍核验/纯项目任务书）×2 项目 | ✓ 全绿 |
| 6 | 重试链修复验证（39s 自愈）+ team room 文件中继 | ✓ |
| 7 | 多维并发（3 team/11 worker：并发供给自愈/真并行派发/中途杀 pod 恢复/挂机补消费） | ✓ 八维度全过 |

### 6.3 分支状态与遗留

- 工作区 clean，与 origin 同步（1418812e）。105 现势镜像：
  cimicode-runtime `v0.1.0-wb105` / bridge `v0.3.4-wb` / operator
  `v0.2.0-wb105` / controller `v1.2.2.12-wb` / dashboard `v1.2.7-wb105`。
- 测试分支 `test/worker-bridge-105` = feature + 105 测试资产 + minio
  service.type（§2.7，**有意保留**，勿盲目合并回 feature）。
- 待办：① dashboard fork 补丁转正（§3.6）；② minio service.type 回迁待
  决策；③ bridge 侧 DM 附件透传（低优先）；④ copaw 侧三个工具缺口
  （delegate_task 容错 / projectflow 标记 action / taskflow deliverables
  nargs）。
