# OpenClaw → 无状态 cimicode 替换:总体方案(汇总版)

> 日期: 2026-08-17 | 状态: 待评审
> 本文是总入口文档,汇总背景、目标架构、分工、可行性、路线图与决策项;细节见文末索引的三份子文档。
> 适用读者: 平台开发(controller)、bridge/cimicode 团队、业务负责人

---

## 1. 背景与目标

**现状**: AgentTeams 平台上每个 agent 常驻一个 OpenClaw worker pod(Node.js gateway + Ubuntu + 本地 fs + Matrix 长连接),不论是否干活都占资源。当前生产环境:10 节点 k8s 集群 + Synapse(Matrix)+ 外接 S3,部署 dev-v1.2.2 线。

**目标**: 用无状态 cimicode(opencode 魔改,"大脑"HTTP 服务 + OpenSandbox 远程沙箱"手脚")替换 OpenClaw,空闲资源大幅下降。

**规模前提(已确认,决定方案生死)**:
- team 数量多,**大量时间空闲**(空闲率是节省公式的主要变量)
- **每 team ≤ 10 个 agent**(单 JVM 规模瓶颈否决项解除)

**核心思路**: cimicode 不连 Matrix、不存历史、不做定时——这些缺口由一个**每 team 一个的 Java Bridge pod** 补上;平台 controller 继续管身份与资源(Matrix bot 账号、房间、S3 凭证),但不再 per-agent 建 worker pod,改为 per-team 建 bridge pod。

---

## 2. 目标架构

```
┌─ AgentTeams 平台(本仓库) ────────────────────────────────────────────┐
│  controller(Go operator) ── Synapse(Matrix 总线) ── S3/OSS ── Higress │
│  职责:Team/Worker/Human/Manager CRD 调谐、Matrix bot 账号与房间、       │
│       S3 用户与策略、网关 consumer、(新)per-team bridge pod 创建       │
└────────────┬─────────────────────────────────────────────────────────┘
             │ 创建/挂凭证
             ▼
┌─ Bridge pod(每 team 1 个,Java,外部资产) ────────────────────────────┐
│  Matrix 监听:N 个 bot /sync 长轮询、@mention 检测路由、协作限流、        │
│             Leader heartbeat(15m)、多身份发言                         │
│  cimicode 编排:上下文组装(历史重放+群聊视野)、调 SSE、响应回 Matrix    │
│  沙箱管理:懒建/done 后 pause/TTL 回收前备份/重建恢复、文件双向同步       │
└───────┬───────────────────────────┬───────────────────────────────────┘
        │ HTTP(SSE)                  │ 生命周期 API
        ▼                            ▼
┌─ cimicode 集群(2-3 pod) ──   ┌─ OpenSandbox Server + 每 agent 一个沙箱 ──┐
│  无状态大脑:/session/        │  手脚:bash/read/write/edit/ls/glob/grep  │
│  context_prompt(SSE)、       │  Running↔Paused、TTL 回收、续期            │
│  内存 session(30min TTL)     │  空闲 pause ≈ 近零资源(POC 验证项)        │
└──────────────────────────────┴───────────────────────────────────────────┘
```

**状态归属**: 会话历史/映射/since-token → S3(建议)或 PG+Redis(待决策 D1);代码与产物 → 沙箱 + S3 备份;房间消息 → Synapse;cimicode 自身零状态。

---

## 3. 三方改造分工

| 方 | 改动量 | 内容概要 | 明细 |
|---|---|---|---|
| **平台(本仓库)** | 中 | Team CR 增 bridge 配置;调谐分流(cimicode 模式建 1 个 bridge pod,成员 infra 照旧不建 worker pod);凭证 REST;finalizer 增沙箱清理钩子;helm 增 cimicodeBridge 段;双 runtime 灰度 | rework 文档 §5 |
| **Bridge(外部)** | 大 | 8 大项 21 条:平台 Pod 契约(entrypoint/readiness/env/凭证 REST)、Matrix 集成(/sync、@mention、多身份、登录风暴退避)、编排(同房串行、协作限流、上下文组装、heartbeat)、沙箱生命周期、健壮性(线程池隔离、SSE 超时、失败重放语义) | rework 文档 §3 |
| **cimicode(外部)** | 小 | 7 条,全为防御性/契约明确化:同 session 并发防御、纯推理段 TTL 续期、认证、SSE 断连探测语义、终态时序、LLM 出口、部署契约 | rework 文档 §4 |

**平台已有资产(不重做)**: RuntimeCimicode 全套接线已在 `feat/cimicode-runtime` 分支(backend 镜像分支、config、两份 CRD 枚举、helm values、manager 模板);Matrix bot provision、S3 用户策略、网关 consumer、REST credentials 端点均为现成能力,bridge 直接消费。

---

## 4. 可行性结论

| 维度 | 判定 | 依据 |
|---|---|---|
| 规模瓶颈 | ✅ 解除 | ≤10 agent ≈ 10 长连接 + ≤5 并发 SSE ≈ 15 线程,JVM 轻松(贴文 §8.4 上限表的否决区在 >20) |
| 资源收益 | ✅ 大概率净省 | 空闲主导 + n(≤10)是放大器:`n×T×P_openclaw` vs `T×P_bridge + 少量常驻 cimicode + 活跃沙箱`;空闲沙箱 pause 后≈近零是关键前提,**POC 实测** |
| 爆炸半径 | 🟡 可接受 | bridge 挂 = 1 个 team 瘫痪(≤10 agent),恢复 20-30s(错峰登录);状态落 S3 后损失收敛为重启窗口内 buffer+进行中 SSE。需业务签字 |
| 功能退化 | 🟡 需签字 | 放弃 dreaming 自动记忆蒸馏、memorySearch RAG;靠手写 memory + 历史摘要兜底 |
| 技术可行性 | ✅ | cimicode 12 项核心能力已实现;平台接线已完成;无架构性未知项,风险集中在 SSE 事务模型(R1/R2)与沙箱 TTL(R4),均有缓解设计 |

**总判定: 方案可行,按 per-team bridge 推进。两道硬门:POC 资源实测(净省 ≥30%)+ dreaming/RAG 放弃的业务确认。**

---

## 5. 实施路线图

```
Phase A  决策敲定(本周)                 ← 阻塞后续所有开发
  □ D1-D6 六项拍板(见 §6)
  □ 三方契约对齐:env 字典、凭证 REST 格式、SSE 事务语义、沙箱生命周期规则

Phase B  验证(1-2 周,决定生死)          ← 与 Phase C 并行可谈,但上线门禁在此
  □ POC:一个 5-10 agent team 跑 48h,量沙箱三态资源/bridge 稳态/总账对比
    成功线:净省 ≥30%;<10% 或反增 → 停,改走 openclaw 降配/sleep-wake
  □ Spike:单 bot 端到端(@mention → SSE → 回房),24h 稳定 + 断线重连

Phase C  开发(2-4 周,三方并行)
  □ 平台:Team CR 扩展 + 调谐分流 + 凭证 REST RBAC + helm 段
  □ Bridge:B1-B20(优先 B3/B6/B7/B9/B11/B14/B16 主链路)
  □ cimicode:C1-C7

Phase D  联调 + 故障注入(上线门禁)
  □ F1-F8:杀 cimicode pod 重放恢复/同房串行/TTL 到期重建/bridge 重启恢复/
          杀 OpenSandbox Server/大 context/协作链拦截/pause-resume

Phase E  灰度 → 全量
  □ 新 team 默认 cimicode,存量 team 保持 openclaw;回退 = 改 Team CR runtime
  □ 观察 1-2 周资源曲线,达标后逐批迁移
```

---

## 6. 待决策清单(Phase A)

| # | 决策 | 建议 |
|---|---|---|
| D1 | bridge 状态存储 | S3-only(平台零新增依赖);若 bridge 改不动 → 平台增部署 PG+Redis |
| D2 | cimicode LLM 出口 | 过 Higress 网关(凭证集中);直连则 key 分发外泄面大 |
| D3 | cimicode 认证 | 随 D2:网关 key-auth / NetworkPolicy 集群内隔离 |
| D4 | bridge 凭证获取 | bridge 调 controller REST 拉取(端点已有,平台近零改动) |
| D5 | E2EE | 明确不支持,团队房保持未加密 |
| D6 | OpenSandbox Server 部署 | 独立 namespace 独立管,网络仅集群内 |

---

## 7. 文档索引

| 文档 | 内容 |
|---|---|
| 本文(`wiki/cimicode-overall-plan.md`) | 总方案:架构/分工/可行性/路线图/决策 |
| [cimicode-rework-and-feasibility.md](cimicode-rework-and-feasibility.md) | Bridge 21 条 + cimicode 7 条改造需求与验收标准、平台侧改动面 |
| [cimicode-runtime-integration.md](cimicode-runtime-integration.md) | 平台侧技术设计(v2):cimicode 事务模型解读、R1-R12 风险清单、Phase 0-5 |
| `wiki/user_pasted_clipboard_long_content_as_file_# OpenClaw → 无状态 cim.txt` | 外部来源的完整分析(贴文方案):架构推演、失败模式 §7.4、资源分析 §8.3 |
