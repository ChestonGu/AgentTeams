# worker-bridge/bridge 代码审查报告

## 1. 审查结论

Bridge 的模块划分、两种 runtime 的适配边界和结构化 prompt 生成方向合理，现有测试也有较好的基础。但异常路径中存在身份判断错误、错误响应被当作成功、旧任务事件混入当前回复和超时失效等问题。建议先修复消息处理的正确性与可靠性，再投入性能优化和模块重构。

本次记录 **14 项可行动问题：8 项 P1、6 项 P2**，另外列出维护性建议。没有发现需要定为 P0 的问题。这里的优先级用于安排修复，不表示每种部署都已发生事故。

- **P1**：可能越过预期的身份边界、暴露不应发送的内容、丢失任务结果，或使任务一直无法结束；建议优先修复。
- **P2**：影响长任务响应、故障反馈、探针可信度或资源管理；建议在生产化完善中处理。
- **维护性建议**：主要降低运行成本和后续修改风险，不单独计入上述问题数量。

**交付范围：本提交仅保存审查报告，没有修改 bridge 实现。** 以下建议仍待实现和回归验证。

## 2. 基线、范围与验证方法

| 项目 | 内容 |
| --- | --- |
| 审查日期 | 2026-10-09，Asia/Shanghai |
| 当前分支 | `dev-v1.2.3`，审查开始时与本地 `origin/dev-v1.2.3` 一致 |
| 审查基线 | `ef3828fadb918bacaa3951e0cf8dc6f5fdf75da7` |
| 基线提交 | `fix(controller): InjectWorkerCoordination detach 路径 role 改按 TeamLeaderName 推导，修复移除成员后协作块畸形` |
| 工作区 | 审查开始时干净 |
| 主范围 | `worker-bridge/bridge` 的应用编排、Matrix、runtime adapters、S3 bootstrap、prompt、HTTP API、状态存储、配置、生成工具与现有测试 |
| 关联范围 | Controller 的 Worker env/runtime 配置投影，以及 worker-bridge 契约和模板；用于核对接线与角色来源 |
| 审查方式 | 全目录静态阅读、现有测试执行、12 组隔离复现、已安装 SDK 源码核对 |

审查针对当前完整实现，并非只检查最近一个提交的差异；报告中的问题不能一概归因于基线提交。没有访问生产 Matrix、S3、Gateway 或 Kubernetes 集群，也没有执行真实 Agent 工具任务。因此，生产发生频率、吞吐量和远端 Gateway 的精确重放行为仍需要联调确认。

### 2.1 实际验证结果

使用独立 Python 3.12.14 虚拟环境，依赖范围遵守 bridge 的 `pyproject.toml`；没有修改系统 Python 或仓库依赖声明。

```bash
cd /Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge
PYTHONDONTWRITEBYTECODE=1 \
  /tmp/agentteams-bridge-review-venv/bin/python -m pytest -q -p no:cacheprovider
```

结果：**134 passed，1 warning，6.33s**。警告来自 Starlette TestClient 对当前 httpx 接入方式的弃用提示。关键依赖实际版本：`matrix-nio 0.26.0`、`httpx 0.27.2`、`httpx-sse 0.4.3`、`pydantic 2.14.0`、`PyYAML 6.0.3`、`minio 7.2.20`、`redis 8.1.0`、`fastapi 0.143.0`、`pytest 9.1.1`。

额外复现脚本在审查时放于 `/tmp/agentteams-bridge-review-probes.py`，未作为项目测试提交。它使用假的传输层、真实的 nio 错误响应类型和 FastAPI TestClient，不连接外部服务；断言的是当前错误行为，不是修复后的验收行为。

| 复现组 | 实际观察 | 对应问题 |
| --- | --- | --- |
| 跨域身份匹配 | 外域同名 sender 被认作 leader；mention 外域同名 worker 也触发自己 | F01 |
| 缺少角色配置 | peer 被归为 human，消息获准执行 | F02 |
| `SyncError` | 两次 sync，refresh 调用 0 次；第二次调用前 connected 仍为 true | F03 |
| `RoomSendError` | send 返回 `None`；应用处理后历史缓冲条数变为 0 | F04 |
| 缺失 baseline | 内部超时设 0.01s，仍需外部 0.06s 的 `wait_for` 才终止 | F05 |
| 多 invocation 重放 | 两次完成文本分别为 `old answer`、`old answer\nnew answer` | F06 |
| reasoning 聚合 | thinking 默认 hide，完成文本却为 `internal reasoning\nanswer` | F07 |
| 未就绪与调试调用 | 未就绪 `/readyz` 返回 200；调试调用后 ready/connected 变为 true，真实 buffer 被清空 | F10、F11 |
| HTTPS S3 endpoint | 输入 `https://s3.example`，SDK 参数却是 `secure=False` | F12 |
| client 热替换 | 替换成功，旧 client 的 close 调用次数为 0 | F14 |
| runtime 抛异常 | 应用捕获异常，但房间发送次数为 0 | F13 |
| 同步 I/O 阻塞 | 注入 0.08s 同步读取，5ms 定时任务实际等待约 0.0854s | F09 |

现有测试通过与这些问题并不矛盾：许多测试覆盖正常返回、单一 invocation 和成功发送；部分探针测试甚至明确断言“未就绪也返回 200”。

## 3. 当前结构与值得保留的设计

当前处理链可概括为：

```text
Matrix sync → 文本回调 → mention/角色过滤 → 读取 runtime.yaml
→ 组装 room history 与当前消息 → v2.4 generator 生成 agent.md
→ stateless submit + SSE / pod REST + polling
→ 聚合 RuntimeEvent → Matrix 回复 → 清空 room history
```

值得保留的部分：

1. `matrix/filter`、`matrix/gateway`、`runtime`、`bootstrap` 和 `prompt` 已有明确边界，适合逐项修复，无需整体重写。
2. Pod 与 stateless adapter 独立，避免把不同协议硬塞进同一个实现；pod 已复用 HTTP client。
3. Generator 使用结构化 runtime 配置和 golden fixtures；不应为优化而回退到从 Markdown 正则抓取团队事实。
4. 初次 whoami 在首次 sync 派发前回填身份，避免首批 mention 因自身 MXID 尚未就绪而漏判。
5. `_gateway_running()` 防止恢复过程中启动两个活跃 sync 循环。
6. Pod 用 baseline 的列表位置截断历史，避免单纯排除某条 ID 导致旧回复反复作为 progress 输出。
7. S3 读取正确关闭 response 并释放连接；常规 shutdown 也会关闭 Matrix client 和当前 runtime client。F14 针对的是热替换路径，不是缺少所有关闭逻辑。
8. Markdown 渲染禁用原始 HTML，有利于控制格式化消息的输入风险。

## 4. 问题总览

| 编号 | 优先级 | 问题 | 验证强度 |
| --- | --- | --- | --- |
| F01 | P1 | MXID 去掉域名，混淆角色身份和 mention 目标 | 已复现 |
| F02 | P1 | 角色映射未从 runtime 快照接线，默认把任意 sender 当 human | 已复现默认行为；核对 env 构建路径 |
| F03 | P1 | Matrix `SyncError` 没有进入刷新/故障处理 | 使用真实 SDK 错误类型复现，并核对 SDK 源码 |
| F04 | P1 | Matrix 发送失败被视为成功，随后清空上下文 | 传输层与应用链路共同复现 |
| F05 | P1 | baseline 不存在时绕过轮询 deadline | 已复现 |
| F06 | P1 | Session SSE 未关联本次 invocation，重放可污染结果 | 合成重放流已复现；远端行为待联调 |
| F07 | P1 | reasoning 被当作回答正文聚合和发送 | 已复现 |
| F08 | P2 | 整轮执行阻塞 Matrix sync 回调，延迟后续消息并扩大重放窗口 | 静态链路与 SDK await 行为核对 |
| F09 | P2 | 同步 S3 与 subprocess 阻塞事件循环 | 隔离延迟复现 |
| F10 | P2 | 就绪探针 HTTP 语义及健康状态不可靠 | HTTP 行为复现、状态路径核对 |
| F11 | P2 | 无鉴权调试入口修改真实生产状态 | TestClient 复现 |
| F12 | P1 | HTTPS S3 地址被默认降为非 TLS 连接 | SDK 构造参数复现 |
| F13 | P2 | 多种失败只写日志，房间没有结果或失败通知 | 异常路径复现 |
| F14 | P2 | runtime client 热替换未释放旧连接池 | close 计数复现 |

## 5. 详细发现

### F01：完整 MXID 被缩减成 localpart，身份边界失效

**位置：** [matrix/filter.py:64](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/matrix/filter.py:64)、[角色匹配:33](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/matrix/filter.py:33)、[mention 匹配:128](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/matrix/filter.py:128)。

`_normalize_user_id()` 去掉 `@`、截掉 `:` 后的域名，再统一转小写。这个函数同时用于 sender 的角色授权与 mention 目标判断。

**触发与影响：** 当房间里存在不同 homeserver 上的同名账户时，`@leader:other.example` 会获得配置给 `@leader:trusted.example` 的 leader 角色；发给 `@worker:other.example` 的结构化 mention 也会触发 `@worker:trusted.example`。攻击者仍需要具备向该房间投递消息的能力；该问题不等于可以绕过 Matrix 的入房权限。

**复现：** 设置自身与 leader 为 trusted 域，在 `m.mentions.user_ids` 中放入完整 MXID。跨域 sender 的结果为 `accepted=True, role=leader`；只 mention 外域同名 worker 的结果也为 `accepted=True`。

**建议：** 将“完整身份解析”和“纯文本短别名解析”拆开。Sender 与结构化 mention 使用完整 MXID；短名称只在文本回退路径中，根据明确的房间成员/域映射解析。不要把短别名用于角色授权。

**验收：** 相同 localpart、不同域的 sender 和 mention 均不能相互替代；合法的同域短 mention 继续可用；对含端口的 server name、URL 编码和非法 MXID 增加边界验证。

### F02：生产角色过滤依赖未接线的 env，且没有明确 Human 名单

**位置：** [app.py:128](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/app.py:128)、[filter.py:47](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/matrix/filter.py:47)、[Controller WorkerEnvBuilder:28](/Users/cheston/workbach/fork/AgentTeams/agentteams-controller/internal/service/worker_env.go:28)、[runtime 配置模型:98](/Users/cheston/workbach/fork/AgentTeams/agentteams-controller/internal/service/runtime_config.go:98)。

`RoleResolver` 只读取 `COORDINATION_LEADER/ADMIN/WORKERS`。未配置三者时，除自己外的任何 sender 都归为 human；设置映射后，既不属于 leader/admin/workers 的 sender 则归为 unknown。模型没有独立 Human 成员集合。

仓库中非文档、非测试的 `COORDINATION_*` 引用仅见于这里的读取；Controller 默认 env builder 没有注入这些值。runtime 快照有团队成员和 channel policy，但应用刷新时只应用 bridge 绑定，并未把成员身份与策略更新到过滤器。用户通过自定义 env/模板补齐映射的部署，需要单独评估。

**触发与影响：** 默认接线下 peer worker 被识别成 human，互相 mention 的阻断失效；手动配上 leader 后，普通 Human 又可能被识别为 unknown 而无法委派。`allow_unknown=False` 无法阻止默认分支的放行，因为该分支返回的是 human。

**复现：** `MentionFilter(user_id='@worker:trusted.example')` 接收 peer 的 `@worker hello`，得到 `accepted=True, role=human`。

**建议：** 根据 controller 投影的可信成员事实建立完整 MXID → role 映射，支持 Human 名单和角色/房间策略。启动与成员 generation 变化时更新；授权检查需要使用足够新鲜的策略快照。目前过滤发生在每轮 S3 刷新之前，接入动态策略时也要调整这一顺序。宽松本地模式应通过显式开关启用。

**验收：** Controller 默认创建的 bridge 中，leader、admin、Human 能发起允许的任务，peer worker 按策略阻断；成员移除或角色变化在规定时间内生效；缺少授权配置不能自动将所有 sender 归为 Human。

### F03：Matrix 错误响应对象被当作成功 sync

**位置：** [matrix/gateway.py:101](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/matrix/gateway.py:101)、[刷新分支:135](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/matrix/gateway.py:135)、[初次认证:69](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/matrix/gateway.py:69)。

已安装 matrix-nio 的 `sync()` 返回类型是 `SyncResponse | SyncError`，`room_send()` 同样返回成功或错误对象。协议错误通常不以 Python exception 的形式交给调用方。

当前循环未检查 `SyncError`，而是通过 `getattr` 跳过 rooms/next_batch，然后执行 `backoff=1.0` 和 `connected=True`。401 刷新只在 exception 分支按字符串匹配触发。

**触发与影响：** Token 失效时，错误对象不会进入 bridge 的刷新链；应用可能持续表示已连接却收不到消息。SDK 自己可能有部分重试行为，但不等于 bridge 已处理 token 轮换。初次 whoami 失败也直接退出；若 runtime client 已存在，`start_background()` 并不会因此自动启动恢复任务。

**复现：** 假 client 返回真实 `SyncError('invalid token', 'M_UNKNOWN_TOKEN')`；运行两次 sync 后，refresh 回调为 0 次，第二次调用前 connected 仍为 true。

**建议：** 显式区分成功响应和错误响应，统一处理 whoami、sync 与发送错误。依据结构化错误码触发刷新；只有成功 sync 才恢复连接状态。检查 SDK 已有的限流重试，避免叠加重复退避；需要桥接处理时尊重 `retry_after_ms`。

**验收：** 覆盖初次认证失败、运行中 `M_UNKNOWN_TOKEN`、刷新成功/耗尽、限流与网络异常；`connected` 和探针反映实际状态；错误响应不能被记录为成功。

### F04：回复没有送达，却被记录成功并清空 history

**位置：** [matrix/gateway.py:189](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/matrix/gateway.py:189)、[app.py:737](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/app.py:737)、[清空 buffer:753](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/app.py:753)。

`send_text()` 对没有 event_id 的发送结果返回 `None`，包括 `RoomSendError`。调用方没有检查返回值，继续写 `reply sent`/`progress sent`，随后清空上下文。

**触发与影响：** 房间禁止发言、权限失效、限流等情况下，工具任务可能已完成，但人类/Leader 看不到结果；日志又误导为发送成功。若重新委派来取得结果，还可能重复执行工具操作。

**复现：** 注入真实 `RoomSendError(..., 'M_FORBIDDEN')`，传输层返回 `None`。应用完整处理一条任务后，原有旁听 history 从 1 条变为 0。

**建议：** 明确发送成功契约，失败转换为结构化错误。把“runtime 完成”与“回复送达”分开记录；发送失败保留待发结果，使用稳定 Matrix transaction ID 重试发送。只在确认送达或明确的 `NO_REPLY` 决策后消费对应上下文。

**验收：** 失败发送不写成功日志、不删除待发送结果；重试发送不重新执行 runtime；部分 progress 已发送、最终回复失败时，已确认部分不会重复发送。

### F05：pod baseline 缺失时，轮询永远跳过 deadline 检查

**位置：** [cimicode_pod_adapter.py:192](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/runtime/cimicode_pod_adapter.py:192)、[continue:197](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/runtime/cimicode_pod_adapter.py:197)、[deadline 检查:226](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/runtime/cimicode_pod_adapter.py:226)。

`_slice_after_baseline()` 找不到 baseline 时返回 `None`；轮询立即 `continue`，无法执行循环底部的超时判断。这与注释“继续等 deadline 兜底”的意图相反。

**触发与影响：** 服务端历史被截断、会话数据变化等情况下，任务一直轮询，typing 与消息回调持续占用。仅发生 404 会走现有 HTTP 错误分支；本问题针对接口正常返回列表但其中缺少 baseline 的场景。

**最小复现：**

```python
adapter = CimicodePodAdapter("http://unused", timeout_seconds=0.01,
                             poll_interval_seconds=0.001)
async def no_messages(session_id):
    return []
adapter._messages = no_messages
# 实际由外部 wait_for 在 0.06s 终止，内部 0.01s deadline 未终止任务。
await asyncio.wait_for(adapter._poll_reply("s", "missing-baseline"), 0.06)
```

**建议：** 每条循环路径都检查单调时钟 deadline，或在外层使用总时限控制。baseline 丢失应作为明确的状态异常处理，不能直接扫描全历史来“补救”，否则会引入旧回复重放。

此外，当前 POST 使用独立 `timeout+30`，轮询再开启新的完整 timeout；stateless 的 HTTP read timeout 也不等于整轮墙钟时限。建议定义统一 turn deadline，并把剩余预算传给各阶段。

**验收：** baseline 缺失、持续空列表、慢 HTTP、持续 SSE 心跳均能在规定总预算内结束，并返回明确的中断原因。

### F06：session 级 SSE 重放没有与本次提交关联

**位置：** [stateless_adapter.py:73](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/runtime/cimicode_stateless_adapter.py:73)、[事件消费:85](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/runtime/cimicode_stateless_adapter.py:85)、[GatewayV2Dialect:119](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/runtime/cimicode_adapter.py:119)。

提交回执中的 `turnId` 仅用于断流文案，`attemptId` 未使用。订阅的是 session 维度事件流，代码注释明确描述“全量重放 + 实时续读”。Dialect 没有按 `inv`/`epoch` 隔离文本，也没有按事件 ID/序号去重。任何 invocation 的 terminal 都可设置 `terminal_seen=True`。

**触发与影响：** 若流包含旧 invocation，旧答案可能混入本轮正文；若连接在旧 terminal 后关闭，本轮甚至可能被误判完成。若不同 invocation 复用 part ID，还可能受到旧 `_closed_parts` 状态的影响。

**复现输入：**

```text
submit receipt: turnId=new-turn, attemptId=new-attempt
old-inv: text.delta(old-part, "old answer")
old-inv: invocation.idle
new-inv: text.delta(new-part, "new answer")
new-inv: invocation.idle
```

**实际输出：** 完成文本依次为 `old answer` 和 `old answer\nnew answer`。应用采用后一个完成文本，旧内容仍留在最终回复中。

**建议：** 与 Gateway 联调确认 receipt、turn.accepted 和 envelope.inv 的对应关系，再按本次 turn/attempt/invocation 过滤与隔离聚合。不能未经确认直接假定 `turnId == inv`。建立重放 cursor/事件去重规则；只承认当前 invocation 的终态，在其终态后结束消费。也应校验业务回执成功状态及必要字段，避免 HTTP 200 的业务失败继续订阅旧会话事件。

**验收：** 覆盖旧 turn 重放、交错 invocation、重复 durable 帧、重连、旧终态先到和缺失当前终态。本问题的本地聚合错误已确认；真实服务是否提供额外的订阅隔离，需要联调证据。

### F07：reasoning 事件直接进入最终回答，thinking=hide 未生效

**位置：** [cimicode_adapter.py:134](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/runtime/cimicode_adapter.py:134)、[part 聚合:173](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/runtime/cimicode_adapter.py:173)、[thinking 配置:73](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/config.py:73)。

`session.next.reasoning.delta` 与正常 text delta 都被翻译为 `TEXT_DELTA`，共享 parts。终态未带全文时，所有 parts 按顺序拼接，应用随后把这些文本发送到 Matrix。

**触发与影响：** 当 Gateway 发送该 reasoning 事件、且最终 terminal 不提供独立 answer 全文时，推理内容会进入对人类可见的回复。默认 `thinking='hide'` 没有保护这一分支。内容是否敏感取决于实际事件，但不应默认与回答合并。

**复现：** reasoning part 为 `internal reasoning`，answer part 为 `answer`，再发送空正文的 `invocation.idle`；得到完成文本 `internal reasoning\nanswer`。

**建议：** 将 reasoning 与 answer 使用不同事件类型/缓冲区，输出层只聚合 answer；明确执行 thinking 策略。补充版本化事件名称的协议样例，避免新增方言在未知分支中被静默丢弃。

**验收：** hide 模式下所有 reasoning delta/ended 形式均不出现在 `body` 或 `formatted_body`；answer 完整，终态携带全文与不携带全文两种情况都验证。

### F08：长任务直接阻塞 Matrix sync，缺少触发事件持久去重

**位置：** [matrix/gateway.py:162](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/matrix/gateway.py:162)、[runtime 等待:709](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/app.py:709)、[since 保存:127](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/matrix/gateway.py:127)、[随机幂等键:106](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/runtime/cimicode_stateless_adapter.py:106)。

Gateway 的文本回调 `await self.on_message()`，应用又等待整轮 runtime 完成。已核对 nio 源码：sync 处理 timeline 时会逐条 await 回调，之后才返回给 bridge 保存 next_batch。

**影响：** 默认 turn 超时可达 3600s，一条长任务会阻止后续 sync 批次的处理，其他房间消息、后续委派和邀请也延后。工具执行或回复完成后、since 落盘之前若进程退出，同批触发事件可能再次出现。

当前 `event_id` 去重只存在于旁听 buffer；触发任务没有持久去重记录。Stateless 每次 chat 生成新 UUID，无法识别“同一 Matrix 事件重放”。因此存在重复执行风险，不能把 since 持久化等同于任务 exactly-once。

**建议：** sync 回调只完成验证与可靠入队，随后推进游标；队列需要有界，并按 runtime session 串行执行。为触发事件保存 accepted/submitted/completed/delivered 状态及稳定幂等键。不要简单地为每条消息 `create_task()`，否则共享 pod session 会并发提交。

解除 sync 阻塞后，还要把 history 改为“消费快照中的消息”，避免任务期间新收到的旁听消息被 `clear(room)` 一并删除。

**验收：** 一个长 turn 期间其他消息继续接收；重复 event_id 不重复提交；在入队、submit、完成、发送、游标保存等阶段注入崩溃，能恢复到正确状态；队列满时有可见反馈。

### F09：同步 S3、生成器和健康探测阻塞 async 主循环

**位置：** [app.py:634](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/app.py:634)、[生成与回写:688](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/app.py:688)、[恢复读取:454](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/app.py:454)、[prompt.py:68](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/prompt.py:68)、[同步探测:260](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/app.py:260)。

Async handler 直接调用同步 MinIO 读写和 `subprocess.run(timeout=60)`；恢复任务也进行同步 S3 读取、同步 HTTP 探测。等待期间事件循环无法调度 typing 续期、其他异步任务和 HTTP 请求分发。

**复现：** 用 `time.sleep(0.08)` 模拟 `S3Bootstrap.load()`；同时启动一个 `asyncio.sleep(0.005)` 定时任务。该定时任务实际延迟约 0.0854s。这个数值仅说明阻塞语义，不是生产延迟基准。

**建议：** MinIO 调用通过有界线程池/`asyncio.to_thread()` 执行，并设置 SDK 网络超时；生成器保留现有契约与 golden 测试，改为异步 subprocess，正确处理超时及子进程回收。健康探测使用可复用 AsyncClient。生成器的 `TimeoutExpired`/启动失败应统一转换为生成错误。

**验收：** 注入慢 S3、慢生成器和慢健康探测时，typing、探针和取消仍在规定延迟内响应；取消不能遗留子进程；线程并发有上限。

### F10：readyz 与健康字段不能可靠表示可处理任务

**位置：** [api/routes.py:23](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/api/routes.py:23)、[start_background:540](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/app.py:540)、[status_payload:760](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/app.py:760)。

`/readyz` 无论 ready 值如何都返回 HTTP 200。`runtime_healthy` 被赋值为 Matrix 是否连接；Matrix 已连接但 runtime client 仍未建时，pod 模式可以保持 ready。Stateless 就绪仅检查 session，未检查实际处理门禁需要的 sandbox/eid。状态字段主要在启动/恢复时赋值，后续连接变化没有持续同步。

**影响与边界：** 按 HTTP 状态判断的探针会将 `{"ready": false}` 视为成功；`/status` 也可能显示过期健康信息。本次没有确认当前默认部署已经用 HTTP `/readyz` 配置 Kubernetes readiness，因此不能据此声称默认集群必然错误放流。

代码注释把 readiness 失败与 Kubernetes 重启联系起来，容易引导后续设计错误：readiness 本身决定就绪/流量接收，进程重启应由 liveness 等机制决定。

**建议：** 明确定义“能够接受并执行任务”的条件，未就绪返回 503；liveness 独立表示进程存活。健康状态来源于当前 Matrix 状态、runtime 可用性和 adapter 所需绑定，提供具体不就绪原因及最后成功时间。

**验收：** 未配置、runtime 不可达、缺少 eid/sandbox、Matrix 失联、恢复成功均返回正确 HTTP 状态和具体原因。修改现有断言未就绪返回 200 的测试。

### F11：调试 HTTP 入口无隔离地修改真实 bridge 状态

**位置：** [api/routes.py:34](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/api/routes.py:34)、[状态改写:65](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/api/routes.py:65)、[Docker 启动:30](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/Dockerfile:30)。

`/api/v1/bridge/handle-message` 默认注册，没有认证或 debug 开关门禁。调用者自己填写 sender；过滤通过后，接口清空真实 history、记录调试 turn，并把 ready/matrix_connected/runtime_healthy 设置为 true。返回 `forwarded=True`，实际并没有调用 runtime。Docker 默认监听 `0.0.0.0:8081`。

**触发与影响：** 能访问该端口的调用者可以污染健康状态和下一轮任务的上下文。这里不声称端口已公开在互联网上；暴露范围取决于 Service、端口转发和网络策略。即便仅供本地调试，改写真正运行中的状态也可能造成误诊。

**复现：** 初始 ready=false、room buffer 有上下文。发送通过过滤的模拟消息后，ready/connected 变为 true、buffer 清空，而 runtime 没有调用。

**建议：** 生产默认关闭模拟接口；确需启用时限制到明确的本地/授权入口，并使用独立调试状态。模拟处理不应伪造连接状态，响应明确表示 simulated；优先复用纯函数做过滤和上下文预览。

**验收：** 默认部署无法调用模拟入口；开启后不会修改生产 history、健康位和真实 turn 记录；sender 字段不会被当作已认证身份。

### F12：HTTPS S3 endpoint 被忽略，可能降级到明文 HTTP

**位置：** [bootstrap.py:204](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/bootstrap.py:204)、[secure 选择:211](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/bootstrap.py:211)、[Controller endpoint 注入:109](/Users/cheston/workbach/fork/AgentTeams/agentteams-controller/internal/service/worker_env.go:109)。

工厂先去掉 `http://`/`https://`，然后仅通过 `AGENTTEAMS_FS_SECURE` 选择 TLS；该变量缺省时为 false。Controller 默认 env builder 注入 endpoint，却未对应注入 secure。

**触发与影响：** 仅配置 `AGENTTEAMS_FS_ENDPOINT=https://...` 时，客户端实际使用 HTTP。HTTPS-only 服务会读取失败；若服务接受 HTTP，请求签名头及对象内容会通过非 TLS 连接传输。S3 secret key 不会因此直接作为明文请求字段发送，但仍违反 endpoint 指定的传输要求。

**复现：** 设定 HTTPS endpoint 和占位 access/secret/bucket，清除其他 env，捕获 Minio 构造参数得到 `secure=False`。

**建议：** 使用 URL 解析获取 scheme、host 和端口；未显式覆盖时从 HTTPS scheme 推导 secure=true。对显式 secure 与 scheme 冲突规定清晰行为，避免悄悄降级；拒绝 SDK 不支持的 path/query。

**验收：** HTTP、HTTPS、无 scheme 地址、显式 secure 覆盖、端口和冲突配置均覆盖；HTTPS 输入不能默认生成 HTTP 连接。

### F13：异常和配置缺失只写日志，已接受的任务缺少失败反馈

**位置：** [app.py:615](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/app.py:615)、[runtime.yaml 缺失:640](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/app.py:640)、[绑定门禁:660](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/app.py:660)、[生成失败:693](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/app.py:693)、[外层异常:755](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/app.py:755)。

只有 adapter 返回 `runtime_error`/`turn_interrupted` 时尝试向房间发送失败消息。Stateless HTTP/SSE 抛出的异常、生成失败、缺少配置和恢复期间的 accepted mention 则只记录日志或直接返回。

**复现：** 假 runtime 的 `chat()` 抛出异常；应用捕获后房间 `send_text()` 调用次数为 0。恢复前的 accepted mention 也只唤醒 recovery，没有排队或可靠保留。

**影响：** Leader/Human 无法分辨任务是否已提交、仍在运行还是被丢弃，可能持续等待或重复委派。日志中的 warning 不构成房间里的用户可见反馈。

**建议：** 建立统一任务结果状态：未提交、提交结果未知、运行失败、运行完成但发送失败。各分支尽量提供可见且简短的反馈；恢复期间按有界队列保留任务。提交超时可能代表远端已受理，应先凭稳定幂等键/回执查询，不要直接生成新键重试执行。

**验收：** 每种已接受消息均有可查询状态或可见反馈；发送失败也进入 F04 的可靠投递路径；错误反馈不暴露原始凭据或完整私有配置。

### F14：绑定漂移的 client 热替换未关闭旧 runtime client

**位置：** [app.py:314](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/app.py:314)、[覆盖 client:339](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/app.py:339)、[已有关闭 helper:395](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/app.py:395)、[pod client:67](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/src/cimicode_bridge/runtime/cimicode_pod_adapter.py:67)。

`_rebuild_runtime_client_on_binding_drift()` 构建新 adapter 后直接覆盖旧对象，没有调用其 close。Pod adapter 持有 AsyncClient，因此已使用过的旧连接池可能失去显式释放机会。常规 shutdown 和部分 recovery 路径已经有关闭逻辑，但热替换没有复用它。

**复现：** 为旧 client 的 close 设置计数，制造 base_url 漂移；替换成功，close 调用 0 次。

**建议：** 采用异步 client 交换流程：构建成功后安全替换，并关闭旧资源；重建失败保留旧 client。明确哪些参数需重建，哪些可原位更新；与 F08 的 session 执行队列协调，避免关闭正在使用的 client。检查 Redis store 的关闭与 typing task 取消后的等待，统一生命周期契约。

**验收：** 多次热更新后旧 client 都被关闭；构建失败不损坏现有服务；任务执行与热更新交错时不会关闭活动连接；必要时保留未变更 endpoint 的 pod session。

## 6. 性能与维护性优化建议

下列建议应排在正确性修复之后；目前没有生产压测数据，因此不承诺具体吞吐或延迟收益。

| 建议 | 依据与预期收益 | 注意事项 |
| --- | --- | --- |
| Stateless 复用 HTTP client | 现在 submit 和 SSE 各创建一个 AsyncClient；复用连接池可减少连接/TLS 开销 | 增加明确 close，分别配置 connect/read/pool timeout，与 F14 一起管理 |
| 减少每轮 bootstrap 读取 | Managed worker 每轮还尝试读 legacy openclaw.json；配置不变时也反复生成与上传 agent.md | 根据 generation/ETag/内容 hash 缓存；授权策略、身份和绑定变化必须及时生效，不能用永久缓存换性能 |
| 有界事件聚合 | Stateless 保存每个 RuntimeEvent，data 又保留原始 envelope；长任务工具输出增加内存 | 在适配器内逐帧聚合结果，限制 bytes、parts、工具输出；如需诊断留存使用受限存储 |
| 管理 room 与调试记录容量 | 单 room 有条数上限，但 room 数和调试 session turns 无 TTL/总量上限 | 退房清理、room TTL、全局字节预算；真实任务去重状态需要独立可靠存储 |
| 明确出站 mention 歧义 | 成员索引以 localpart/显示名映射单个 MXID，同名成员存在覆盖/歧义；缓存失效时也可能继续用旧索引 | 完整 MXID 优先，短别名只在唯一匹配时解析；成员变化触发刷新 |
| 清理无效配置 | retry/queue/history persistence/shutdown/emitter 的多个选项尚未接线，matrix.since_persist 也未决定是否传 store | 区分已实现与预留项；无法执行的设置应报错或明确告知，避免运营误以为已启用 |
| 修正 SPI 类型契约 | `RuntimeAdapter.chat(request)` 与实际关键字参数不一致；stateless 没实现完整 health/capabilities | 统一请求/结果类型和能力声明，减少 `Any`、duck typing 和隐式属性补丁 |
| 删除重复和过期结构 | `events.py` 重复定义 MatrixMessage；旧 build_agent_md 和 README 的 runtime 文件路径已偏离主链路 | 先核对引用再清理，保留 generator 唯一路径；不把清理与可靠队列重构混成一次大改 |
| 提升 FileStore 持久化质量 | 当前直接 write_text，全量覆盖，TTL 忽略；中途退出可能留下不完整 JSON | 明确仅调试使用，或使用临时文件+原子替换与序列化访问；不要视为任务事务存储 |

### 6.1 文档与实现漂移

[bridge README:14](/Users/cheston/workbach/fork/AgentTeams/worker-bridge/bridge/README.md:14) 仍描述旧 `POST /v1/gateway/session/chat`，并称只复用 S3 openclaw.json 里的预建 session。实际 stateless 使用 Gateway v2 submit/events 双接口，pod 则自行创建 session；runtime.yaml 已成为主要配置载体。

README 还列出不存在的 runtime/client.py、adapters.py、turn.py；S3 路径说明与当前 prefix 处理不同。接口契约中也存在历史版本说明，需要明确哪个版本对应当前 adapter。建议把两种 adapter 的配置来源、请求样例、失败语义和恢复方式分别写清楚，并标出预留能力。

### 6.2 可观测性

现有决策日志和 agent.md hash 有价值，但日志中的 accepted、runtime complete、reply sent 尚不足以表示任务真的交付。建议稳定关联以下字段：

```text
worker / room_id / matrix_event_id / runtime_session_id
submit_idempotency_key / gateway_turn_id / attempt_id / invocation_id
queued / submitted / runtime_completed / delivered / failed_stage
queue_wait_ms / runtime_ms / delivery_ms / retry_count
```

增加队列长度、最后成功 sync 时间、runtime 可用状态、发送失败计数和丢弃原因；正文日志应有明确长度限制与留存策略。不同阶段使用不同名称，避免把“执行完成”误报成“用户已收到”。

## 7. 建议修复顺序与验收计划

### 阶段一：小范围修复确定性缺陷

优先 F01、F03、F04、F05、F07、F12。它们均有明确局部触发条件，适合独立修改和回归。并限制 F11 的生产调试入口；修正 F10 的状态与探针语义。

验收重点：完整身份、真实 nio 错误对象、失败发送不丢结果、所有循环分支超时、reasoning 与 answer 分离、HTTPS endpoint 正确传输。已有正常链路测试必须继续通过。

### 阶段二：补齐协议关联与动态授权

处理 F02 和 F06，分别对接 controller 的成员/策略快照与 Gateway 的 turn/attempt/invocation 关联规则。这两项涉及跨模块契约，应先获得实际 payload 样例，再实现匹配规则。

验收重点：成员变更、Human 与 peer 权限、旧事件重放、重复事件与不同 invocation 的 part ID 隔离。不能仅用当前单 invocation fixture 宣布 SSE 重放问题已解决。

### 阶段三：可靠任务队列与异步 I/O

处理 F08、F09、F13、F14，形成有界队列、每 session 串行执行、稳定幂等键、执行状态与待发结果保存、正确资源关闭。分批引入，避免同时改变协议、prompt 和生命周期而难以定位回归。

验收重点：长 turn 期间收消息、崩溃恢复、发送重试、配置热更新、慢存储和取消。跨 Matrix 与远端 runtime 的原子事务无法靠单个 since token 获得，应以可恢复状态和服务端幂等保证减少重复执行。

### 推荐补充的回归场景

| 场景 | 应验证的结果 |
| --- | --- |
| 外域同名 leader/worker | 不获得本域身份权限、不误触发 |
| 默认 Controller 接线与成员移除 | 角色策略正确且动态更新 |
| whoami/sync 返回错误对象 | 刷新、退避和健康状态正确 |
| progress 成功、最终发送失败 | 保存待发结果，重试不执行第二遍任务 |
| baseline 丢失、HTTP 慢响应、SSE 长心跳 | 总 deadline 生效 |
| 旧 invocation 重放与重复 durable frame | 本轮答案不污染、不误完成 |
| reasoning + answer + 空正文 terminal | thinking hide 不泄露 reasoning |
| 运行期间 Matrix 断线、runtime 未建 | readyz 503，status 原因准确 |
| 默认部署调用模拟接口 | 禁止或隔离，不污染真实状态 |
| HTTPS endpoint 未指定 secure | TLS 仍开启 |
| runtime 异常与恢复期委派 | 有可查询状态或可见反馈，任务不静默消失 |
| 热更新与 shutdown | client、Redis、typing task、子进程被正确释放 |
| 各关键阶段进程退出并重启 | 同 event_id 可恢复，不盲目重复工具执行 |

## 8. 报告使用边界

本报告的确定性判断来自当前基线源码、真实 SDK 类型和隔离复现。合成 SSE 流证明当前聚合器缺少隔离能力，但远端 Gateway 的具体重放/关闭策略仍需验证；HTTP 调试入口的代码缺陷已确认，实际网络暴露程度仍取决于部署。

测试通过只证明现有 134 个场景没有回归。报告没有宣称生产已经发生身份冒用、TLS 数据泄露或重复工具执行，也没有把性能建议当作已测得的性能提升。
