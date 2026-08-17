# cimicode 最终集成:外部组件改造清单与可行性(平台视角)

> 日期: 2026-08-17
> 范围声明: **Bridge(Java 桥接服务)与 cimicode 无状态服务为外部已有资产,不在本仓库、不可提供源码**。本文只从 AgentTeams 平台视角列出两者的改造需求、验收标准与可行性结论;平台侧(controller)改造仅列契约与改动面概要。
> 规模前提(已确认): **team 数量多、大量时间空闲;每 team ≤ 10 个 agent**。
> 依据: `wiki/user_pasted_clipboard_long_content_as_file_# OpenClaw → 无状态 cim.txt`(下称"贴文方案",描述最终运行环境)、`wiki/cimicode-runtime-integration.md`(本仓库 v2 设计,R1-R12 风险清单)。
> 基线分支: `feat/cimicode-runtime`(controller 侧 RuntimeCimicode 接线已完成,Java 桥源码已删除)。

---

## 0. 结论(TL;DR)

1. **规模前提解除了贴文方案的两个最致命否决项**:每 team ≤10 agent → 单 JVM 约 10 个 Matrix 长连接 + ≤5 并发 SSE ≈ 15 活线程,bridge 规模瓶颈不存在;team 大量空闲 → 沙箱 pause/回收正是资源最省的场景。**per-team bridge 模型在本规模下可行**。
2. Bridge 改造共 **8 大项**(§3),cimicode 改造共 **7 项**(§4),多数是防御性与契约明确化,无架构性重写。
3. 平台侧核心改造一件事:**Team 调谐分流——cimicode 模式下建 1 个 team 级 bridge pod,不再 per-agent 建 worker pod**(§5)。本分支已有的 RuntimeCimicode 接线(镜像/env/CRD 枚举)大部分可复用。
4. 资源收益成立的两个硬条件(POC 实测):**空闲沙箱 pause 后资源足够低** + **bridge 自身空闲开销可控**(可选缩容,§6.3)。
5. 三方契约必须先行(§8 决策清单):凭证下发方式、bridge 状态存储选型、cimicode 认证与 LLM 出口。

---

## 1. 最终架构与职责边界

```
AgentTeams 平台(本仓库,我方)                外部资产(不可改源码,只提改造需求)
─────────────────────────────────           ─────────────────────────────────
controller(Go operator, :8090)   ──创建──▶  Bridge pod(每 team 1 个,Java)
Synapse(Matrix 消息总线)        ◀──sync──   · N 个 agent bot 的 /sync 长轮询
S3/OSS(对象存储)                ◀──读写──   · @mention 路由 / 协作限流 / heartbeat
Higress 网关(LLM 出口,待定)     ◀──SSE───   · 沙箱生命周期管理
helm 部署体系                               · 上下文组装(历史 + 群聊视野)
                                            │
                                            ▼ HTTP(SSE)
                                            cimicode 集群(2-3 pod,无状态大脑)
                                            │ sandboxID
                                            ▼
                                            OpenSandbox Server + 每 agent 一个沙箱
```

职责切分原则:**平台管身份与资源(Matrix bot 账号、房间、S3 凭证、pod 生命周期),bridge 管运行时行为(消息路由、会话、沙箱),cimicode 只管推理**。

---

## 2. 平台→bridge 的既有能力(改造的对齐目标)

平台 Provisioner/Deployer 已为每个 agent 提供以下资产,bridge 的改造就是消费它们:

| 资产 | 平台现状 | 位置 |
|---|---|---|
| Matrix bot 账号 | AppService 模式 provision,token 无密码 | `internal/service/provisioner.go` ProvisionWorker |
| Matrix 凭证 | Secret `agentteams-creds-<name>`(MATRIX_TOKEN/PASSWORD 等) | `internal/service/credentials.go` |
| S3 凭证与策略 | 每 agent 独立 MinIO/OSS 用户,策略限 `agents/<name>/*` + `teams/<team>/*` | `internal/service/worker_policy.go` |
| 团队房间 | `#agentteams-team-<name>`,成员/权限等级由 controller 收敛 | ProvisionTeamRooms |
| 协作上下文 | leader/worker 的 AGENTS.md、HEARTBEAT.md、channelPolicy 注入 S3 | `internal/service/deployer.go` |
| LLM 凭证 | 网关 consumer key(每 agent 一个) | EnsureWorkerGatewayAuth |
| REST 取数接口 | `/api/v1/workers|teams` + credentials/lifecycle 端点,SA TokenReview 鉴权 | `internal/server/http.go` |

---

## 3. Bridge 改造清单(Java,外部——只列需求)

### 3.1 平台 Pod 契约(新增,贴文未覆盖)

| # | 改造项 | 要求 | 验收 |
|---|---|---|---|
| B1 | **entrypoint/生命周期** | 作为 controller 创建的 pod 运行:启动 → 加载 team 配置 → 初始化(见 B4)→ readiness → 进入监听;收到 SIGTERM 等进行中 SSE 完成或超时后再退出 | 滚动重启不丢进行中响应(或明确放弃) |
| B2 | **readiness 协议** | 就绪信号 = 全部 bot /sync 建立且 cimicode Service 可达。两种方式二选一:镜像内置 `agt worker report-ready`(平台标准)或 HTTP readinessProbe | Team status 不因 bridge 假 ready 卡住 |
| B3 | **凭证获取方式** | 平台按 agent 逐个发 Secret,team 级 bridge 需要聚合。二选一:(a) controller 渲染 team 级聚合 Secret 挂载(平台需新增);(b) bridge 持 K8s SA / controller token,调 REST `/api/v1/workers/{name}/credentials` 拉取(端点已有)。**建议 (b),平台零改动** | bridge 重启后凭证可再获取 |
| B4 | **启动 catch-up** | 先完成 initial /sync 再处理增量,防止重启窗口丢消息(平台 P5 惯例,参见 manager_reconcile_welcome.go 注释) | 重启期间发的 @mention 不丢 |
| B5 | **env 契约** | 消费平台注入的环境变量命名(`AGENTTEAMS_*`):MATRIX_URL、FS/OSS endpoint+AK、CONTROLLER_URL 等;具体清单以平台 `worker_env.go` 输出为准,双方对齐一份 env 字典 | 无硬编码地址/密码 |

### 3.2 Matrix 集成(贴文 §6.2 已识别,按此验收)

| # | 改造项 | 要求 | 验收 |
|---|---|---|---|
| B6 | **/sync 长轮询** | eager 启动每 bot 一条;backoff 重连;since-token 增量;token 过期重登录(新 device) | 24h 稳定,断网恢复自动续传 |
| B7 | **@mention 检测/路由** | 解析 body 中 `@bot:domain` → 映射 agent 身份;发送者白名单校验(channelPolicy 已在平台侧配置,bridge 按 S3 下发的 AGENTS.md/channelPolicy 执行) | 多 agent 同房间互不误触发 |
| B8 | **多身份发言** | 以被 @ 的 bot 身份 sendMessage;维护 displayName | Element 里各 agent 名字正确 |
| B9 | **登录风暴退避** | bridge 重启时 N 个 bot 同时重登录会打爆 Synapse rc_login(429,踩过的坑)。必须错峰(如 2s 间隔)+ 429 退避 | 重启不触发 429 风暴 |
| B10 | **E2EE 决策** | 平台房间当前未启 E2EE。若最终环境要启,bridge 须落地 crypto store 并处理多 device 协商;**建议明确声明不支持 E2EE,平台房间保持未加密** | 书面确认 |

### 3.3 编排与上下文

| # | 改造项 | 要求 | 验收 |
|---|---|---|---|
| B11 | **同房间串行** | 同一 room 的消息严格串行处理(R2 在桥侧的防线,cimicode 同 session 并发防御依赖此) | 同房连发 5 条不并发 |
| B12 | **协作限流** | 默认仅 Leader/Admin/Human 的 @ 触发 worker;worker 回复中的 @ 不触发(平台 groupAllowFrom 语义复刻);可选 peer-mentions + 深度上限/时间窗/去重/自 @ 过滤 | 无死循环:A→B→A 被拦 |
| B13 | **上下文组装** | 与 cimicode 事务模型对齐:每轮全量 context 重放 + 新消息 parts;**按任务切 session**(R5 成本曲线)+ 历史摘要压缩;群聊视野两段式格式(贴文 §2.3) | 长会话请求体不失控(告警阈值) |
| B14 | **状态持久化选型** | 贴文设计 = PG(历史)+ Redis(sessionID↔sandboxID、since-token)。平台环境无 PG/Redis → **建议改为 S3**(state.json + export 回放,本仓库 v2 设计已验证格式);若 bridge 无法去掉 PG/Redis,平台需增部署,运维面+2(见 §8 决策 D1) | bridge 重启后映射/历史/since 可恢复 |
| B15 | **Leader heartbeat** | 定时器(默认 15m)注入 HEARTBEAT.md 调 leader session;heartbeat 期间 leader @worker 走正常触发 | 心跳报告出现在团队房 |

### 3.4 沙箱管理层

| # | 改造项 | 要求 | 验收 |
|---|---|---|---|
| B16 | **生命周期规则** | 首次 @ 到懒建;done 后 N 分钟 pause;长期空闲 TTL 回收**前**备份 `outputs/ shared/ memory/` 到 S3;再 @ 到重建+恢复;每轮 done/里程碑沙箱内 git commit(对冲 R4:TTL 回收=代码全灭) | 沙箱被回收后代码可从 S3/git 恢复 |
| B17 | **文件交换** | 调 cimicode 前同步团队共享文件(`teams/<name>/shared/`)进沙箱;done 后产物同步回 S3 | agent 间 result.md 协作链路通 |

### 3.5 健壮性

| # | 改造项 | 要求 |
|---|---|---|
| B18 | **线程模型** | Matrix /sync 线程组与 SSE 消费线程组隔离;SSE 用专用**有界**线程池(禁 ForkJoinPool.commonPool);OkHttp 连接池 ≥ N+M |
| B19 | **SSE 超时** | 禁 readTimeout=0;加 stream 总超时(建议 ≥ 最长任务时长)+ 心跳(10s)缺失判定 |
| B20 | **失败语义** | SSE 断连/沙箱终态/TTL 到期 → 走"重建+context 重放"恢复路径,不向用户裸报错(贴文 R9) |

### 3.6 Bridge 可选优化(v2,空闲多的场景价值高)

| # | 项 | 说明 |
|---|---|---|
| B21 | **空闲缩容** | team 空闲时 bridge 是纯固定开销(Java 常驻)。可选:bridge 空闲 M 小时自动退出,靠平台 controller 感知 pod 消失重建(冷启动 ~10-20s),或后续用 Synapse appservice push 唤醒。**先不做,量出 bridge 空闲成本再定** |

---

## 4. cimicode 服务改造清单(外部——只列需求)

| # | 改造项 | 需求 | 来源 |
|---|---|---|---|
| C1 | **同 session 并发防御** | 同 sessionID 已有活跃 SSE 时拒绝第二个请求。多 Pod 下无共享状态,若做不了跨 Pod 防御,须**书面声明依赖调用方(bridge)同房间串行**(B11),并文档化 | R2 |
| C2 | **纯推理段 TTL 续期** | 每轮 prompt 开始即 renewTTL(现仅工具调用成功后续期;长无工具推理 → TTL 到期 → 下次工具调用遇终态) | R6 |
| C3 | **认证** | `/session/context_prompt` 目前无认证,谁能 POST 谁就能烧 LLM 额度。二选一:走网关加 consumer key-auth,或声明"仅集群内网络可达+NetworkPolicy 隔离" | R8 |
| C4 | **SSE 断连探测语义** | 明确"断连"探测机制(TCP FIN vs 10s heartbeat 超时),孤儿内存 session 的存活上限(heartbeat 周期 + 30min TTL 兜底) | R1 |
| C5 | **终态时序** | `done` 事件与内存 session 删除、客户端缓冲刷出的顺序保证;`session.error` 终态的错误分类(可重放恢复 vs 不可恢复) | R1/R9 |
| C6 | **LLM provider 出口** | 对接平台 Higress 网关(baseURL + 每 agent consumer key)或声明直连 LLM(平台侧放行)。请求级 `model` 与启动级 provider 配置的优先级需给出结论 | R8 |
| C7 | **部署契约** | 优雅终止 ≥ 最长任务;preStop 等活跃 SSE;内存按"并发 SSE × 平均 session 大小"估算;HPA 按 SSE 并发数(不按 CPU);文档化 `background bash 在 done 后仍运行` 的审计语义 | wiki §3.5 |

另外两个**待 cimicode 侧确认的契约问题**(不一定是改造):文档占位符 `glm-5.3_common` 是否笔误;InMemorySessionStore 事件格式与持久 session 的差异边界。

---

## 5. 平台侧改造概要(本仓库,后续工作)

> 只列改动面,不是今天的实现范围。贴文"难点一"映射到本仓库真实文件:

1. **Team CR 扩展**:`TeamSpec` 增 bridge 配置段(runtime 标记、bridge image、资源、cimicode URL 等);`TeamStatus.Members[].Ready` 语义变为"bridge 就绪 + 该 agent 沙箱/会话可用";增 bridge pod 状态字段。改 `api/v1beta1/types.go` + 两份 CRD yaml(`config/crd/` 与 `helm/agentteams/crds/` 必须同步,CI 有 check)。
2. **调谐分流**:[team_controller.go](../agentteams-controller/internal/controller/team_controller.go) `reconcileTeam` 按 runtime 分流:cimicode 模式下成员仍走 `ReconcileMemberInfra`(Matrix bot/凭证/S3 **照旧**,这是平台价值所在),但 `ReconcileMemberContainer`([member_reconcile.go](../agentteams-controller/internal/controller/member_reconcile.go))跳过 per-agent pod;新增 team 级 bridge pod 创建(可复用 `backend/kubernetes.go` 的 Create + pod-template overlay)。
3. **凭证聚合**:若走 B3(b) 方案,bridge 用 SA 调 controller REST,平台只需给 bridge pod 的 SA 发相应 RBAC/audience(平台 auth 已支持 TokenReview)。
4. **finalizer 链路**:`handleDeleteTeam` 增"通知 bridge 清理沙箱"钩子(bridge 提供 shutdown-hook API 或读 S3 墓碑标记);CRD 驱动的 bridge pod 随 OwnerReference 自动回收。
5. **helm**:`values.yaml` 增 `cimicodeBridge` 段(镜像/资源/env/cimicode URL);本分支已有的 `worker.defaultImage.cimicode` 接线可复用。
6. **双 runtime 灰度**:openclaw team 与 cimicode team 共存互不干扰;回退路径 = Team CR 改 runtime 回 openclaw(per-agent 接线仍在,天然回退方案)。

---

## 6. 可行性结论(按已确认规模:team 多、大量空闲、每 team ≤10 agent)

### 6.1 规模瓶颈 —— 解除 ✅

贴文 §8.4 的上限表:10 agent ≈ 15 活线程 = "没问题"。≤10 agent/team 的前提下,单 JVM 承载毫无压力;>20/>50 的否决场景不存在。

### 6.2 爆炸半径 —— 可接受 🟡

bridge 挂 = 1 个 team(≤10 agent)瘫痪,恢复 N×2-3s(错峰登录后 10 agent 约 20-30s)。对比 OpenClaw 的"挂一个 pod 只影响一个 agent"确实变差,但:规模小 + K8s 自动重启 + 状态已在 S3(B14)→ 损失收敛为"重启窗口内丢失群聊视野 buffer + 进行中 SSE 半截响应"。**接受此风险是本方案的前提之一,需业务签字。**

### 6.3 资源账 —— 空闲主导场景大概率净省 ✅(POC 确认)

设:每 team agent 数 n(≤10)、team 数 T、空闲率 f(高)、
- OpenClaw:`n×T` 个常驻 pod(Node gateway+Ubuntu,经验值 300-500Mi/个)
- 新模型:`T` 个 bridge pod(Java,估 512Mi-1Gi)+ 2-3 个共享 cimicode pod + `f×n×T` 个沙箱常 Running + `(1-f)×n×T` 个沙箱 paused(≈ 近零,待 POC)+ OpenSandbox Server 固定开销

粗公式:`节省 ≈ n×T×P_openclaw − [T×P_bridge + k×P_cimicode + f×n×T×P_sbx + P_server]`。

**n 是放大器**:n=5~10 时第一项是 bridge 项的 5-10 倍,只要沙箱 paused 足够轻,空闲为主 → 净省成立。两个反项要有数:① bridge 空闲常驻(Java ~0.5-1Gi × T,B21 缩容对冲);② OpenSandbox Server 固定成本。**结论与贴文相反的原因就是规模前提:贴文按"常忙/中等"推演,本环境是"大量空闲+每 team 满配 agent 数"。**

### 6.4 功能退化 —— 需签字 🟡

放弃 dreaming 自动记忆蒸馏、memorySearch RAG(依赖 dreaming)。缓解:手写 memory/ + 历史摘要(B13)。**需业务确认 agent 任务不依赖跨日自动记忆。**

### 6.5 总判定

**方案可行,按 per-team bridge 推进。** 前置两件硬验证(§7),一项业务签字(6.4)。

---

## 7. 验证计划(合并贴文 §9 与本仓库 Phase 5)

### 7.1 POC 资源实测(先行,决定生死)

- 一个 team(5-10 agent)跑 48h,记录:OpenSandbox Running/Paused/Terminated 三态资源、bridge pod 稳态资源、cimicode 集群随并发变化、总账 vs 同规模 openclaw team
- **成功线:净省 ≥30%;<10% 或反增 → 停,改走 openclaw 降配/按需启停(sleepWorker/wakeWorker 已有)**

### 7.2 Bridge spike(与 POC 并行)

单 bot 进房 → @mention → context_prompt → SSE 回房,24h 长连接稳定性 + 断网重连 + since-token 恢复(对应 B6/B7 验收)。

### 7.3 故障注入清单(上线门禁)

| # | 场景 | 期望 |
|---|---|---|
| F1 | SSE 中途杀 cimicode pod | bridge 断点重放恢复,不裸报错 |
| F2 | 同房间连发 5 条消息 | 严格串行,无双开 session |
| F3 | 沙箱 TTL 调 60s 到期 | 终态 → 重建 → S3 恢复 → 重放 |
| F4 | 沙箱 pause → 再 @ | 自动 resume 路径通 |
| F5 | bridge pod 重启 | S3 恢复映射/历史/since,错峰登录无 429 |
| F6 | 杀 OpenSandbox Server | 沙箱数据面存活情况明确,恢复后可用 |
| F7 | 大 context(>1MB 请求体) | 网关/超时行为有数,不静默截断 |
| F8 | 协作链 A→B→A | 被限流拦截(B12) |

---

## 8. 待决策清单(阻塞项,需拍板)

| # | 决策 | 选项与建议 |
|---|---|---|
| D1 | bridge 状态存储 | (a) S3-only(state.json+export,平台零新增,**建议**)/(b) 平台增部署 PG+Redis(bridge 不改,运维面+2) |
| D2 | cimicode LLM 出口 | (a) 过 Higress 网关(凭证集中,平台一致性好,**建议**)/(b) 直连(简单,但 key 分发外泄面大) |
| D3 | cimicode 认证 | 网关 key-auth(随 D2-a)/ NetworkPolicy 集群内隔离(D2-b 时必选) |
| D4 | bridge 凭证获取 | (a) controller 渲染 team 级聚合 Secret / (b) bridge 调 controller REST 拉取(**建议**,平台近零改动) |
| D5 | E2EE | 建议明确不支持,团队房保持未加密(书面确认) |
| D6 | OpenSandbox Server 部署归属 | 平台 helm 增加 vs 独立部署(建议独立 namespace 独立管,网络仅集群内) |
