# 成员协调图

> 根据 HiClaw 代码库（`docs/declarative-resource-management.md`、`manager/agent/team-leader-agent/`、`manager/agent/skills/team-management/`）整理的成员协调关系。
>
> 互补文档：[成员调谐流程](./member-reconcile-flow.md) 讲「单个成员如何被 Controller 调谐上线」（Infra → ServiceAccount → Config → Pod → 端口暴露）；本文讲「成员之间如何通信与委派」。

## 1. 组织结构与协调关系

```mermaid
flowchart TB
    subgraph Human["👤 人类层"]
        Admin["Admin 全局管理员<br/>(Human CRD, Level 1)"]
        TeamAdmin["Team Admin 团队管理员<br/>(spec.admin，默认=全局 Admin)"]
        Coordinator["Human Coordinator 协调员<br/>(spec.humanMembers, role=coordinator)"]
        HumanUser["Human Users 普通用户<br/>(Level 2/3)"]
    end

    subgraph Manager["🤖 管理层"]
        Mgr["Manager Agent 协调者<br/>任务路由 / 团队编排 / 心跳监控"]
    end

    subgraph TeamA["📦 团队 A (alpha-team)"]
        LeaderA["Team Leader A<br/>特殊 Worker（role=team_leader）<br/>分解任务 · DAG 编排 · 聚合结果"]
        WA1["Worker A1"]
        WA2["Worker A2"]
    end

    subgraph TeamB["📦 团队 B"]
        LeaderB["Team Leader B"]
        WB1["Worker B1"]
    end

    WorkerC["Worker C<br/>独立 Worker（不属于任何团队）"]

    Admin -->|下达任务| Mgr
    TeamAdmin -->|Leader DM 直接派任务| LeaderA
    Mgr -->|Leader Room 派发任务| LeaderA
    Mgr -->|Leader Room 派发任务| LeaderB
    Mgr -->|主房间派发任务| WorkerC
    LeaderA -->|Team Room / Worker Room 委派子任务| WA1
    LeaderA -->|Team Room / Worker Room 委派子任务| WA2
    LeaderB -->|委派子任务| WB1
    Coordinator -->|Team Room 派任务| LeaderA

    style Admin fill:#ffe0cc,stroke:#cc6600
    style TeamAdmin fill:#ffe0cc,stroke:#cc6600
    style Coordinator fill:#ffe0cc,stroke:#cc6600
    style Mgr fill:#d4e6ff,stroke:#3366cc
    style LeaderA fill:#e6ffe6,stroke:#339933
    style LeaderB fill:#e6ffe6,stroke:#339933
    style WorkerC fill:#f2f2f2,stroke:#666666
```

**角色说明：**

| 成员 | 角色 | 职责 |
|------|------|------|
| Admin | 全局人类管理员 | 向 Manager 下达任务；可进入各房间监督 |
| Manager | AI 协调者 | 任务路由、Worker/Team 编排；**只与 Leader 对接，绝不直接指挥团队 Worker** |
| Team Leader | 特殊 Worker | 接收 Manager / Team Admin 的任务，分解、DAG 编排、委派、聚合回报 |
| Worker | 执行单元 | 执行子任务，向 Leader 汇报；`spec.peerMentions=true` 时可在 Team Room 互 @ |
| Team Admin | 团队专属管理员 | 通过 Leader DM 直接向 Leader 派任务，Manager 不参与 |
| Coordinator | 人类协调员 | 加入 Team Room，可像 Team Admin 一样在团队内派任务 |

## 2. Matrix 房间通信拓扑

```mermaid
flowchart LR
    subgraph Rooms["📡 Matrix 房间"]
        MainRoom["主房间<br/>Human + Manager + Worker<br/>(独立 Worker 协作)"]
        LeaderRoom["Leader Room<br/>Manager + Global Admin + Leader<br/>← Manager 仅与 Leader 对接"]
        TeamRoom["Team Room<br/>Leader + Team Admin + W1 + W2 + …<br/>← **不包含 Manager**（委派边界）"]
        WorkerRoom1["Worker Room<br/>Leader + Team Admin + Worker"]
        LeaderDM["Leader DM<br/>Team Admin ↔ Leader"]
    end

    Mgr["Manager"] --- MainRoom
    Mgr --- LeaderRoom
    Admin["Global Admin"] --- LeaderRoom
    Admin --- MainRoom
    Leader["Leader"] --- LeaderRoom
    Leader --- TeamRoom
    Leader --- WorkerRoom1
    TeamAdmin["Team Admin"] --- TeamRoom
    TeamAdmin --- LeaderDM
    Leader --- LeaderDM
    Worker1["Worker"] --- TeamRoom
    Worker1 --- WorkerRoom1

    style LeaderRoom fill:#fff2cc,stroke:#cc9900
    style TeamRoom fill:#d9ead3,stroke:#38761d
    style LeaderDM fill:#ead1dc,stroke:#a64d79
```

**关键设计**：Team Room **不包含 Manager** —— 这是委派边界。Manager 通过 Leader Room 与 Leader 沟通，团队内部协调由 Leader 全权负责。

## 3. 任务协调时序

```mermaid
sequenceDiagram
    autonumber
    participant Admin as Admin 管理员
    participant Mgr as Manager
    participant Lead as Team Leader
    participant W as 团队 Worker

    rect rgb(240, 248, 255)
        Note over Admin,W: 路径 A：Manager 委派（跨团队任务）
        Admin->>Mgr: 下达任务
        Mgr->>Mgr: 匹配团队领域，创建任务 spec<br/>(shared/tasks/{id}/, meta.json + spec.md)
        Mgr->>Lead: Leader Room @mention 派发任务
        Lead->>Lead: team-coordination 决定模式<br/>(DAG / Loop / 简单任务)
        alt 简单任务
            Lead->>W: 直接委派给单个 Worker
        else 复杂任务 (Project Mode)
            Lead->>Lead: plan_dag / plan_loop 创建执行计划
            Lead->>W: ready_nodes 逐个委派子任务
        end
        W-->>Lead: 汇报子任务结果
        Lead->>Lead: 评估 / 校验 / 重规划 / 聚合结果
        Lead->>Lead: 写回 result.md → MinIO
        Lead-->>Mgr: Leader Room @mention 汇报完成
        Mgr-->>Admin: 通知任务完成
    end

    rect rgb(255, 248, 240)
        Note over TeamAdmin,Lead: 路径 B：Team Admin 直接任务（团队内部）
        TeamAdmin->>Lead: Leader DM 直接派任务（无 parent task）
        Lead->>W: 团队内部分解委派
        W-->>Lead: 汇报结果
        Lead-->>TeamAdmin: Leader DM 回报完成（Manager 不参与）
    end
```

## 4. 协调决策流（Team Leader 内部策略层）

```mermaid
flowchart TD
    Start["收到任务 / 结果 / 阻塞 / 请求变更"] --> Strat["team-coordination<br/>（策略层：选择 DAG 还是 Loop）"]
    Strat -->|"有限、依赖可规划"| DAG["DAG 模式<br/>clarify → 设计 → 委派 → 收集 → 评估"]
    Strat -->|"重复直到停止条件"| Loop["Loop 模式<br/>clarify → 定义 → 委派 → 评估迭代"]
    DAG --> PM["project-management<br/>plan_dag / ready_nodes"]
    Loop --> PM2["project-management<br/>plan_loop / ready_loop_nodes"]
    PM --> TM["task-management<br/>委派子任务 / 检查结果"]
    PM2 --> TM
    TM --> W["Worker 执行"]
    W -->|结果| Eval["评估：通过 / 修复 / 重规划 / 完成"]
    Eval -->|需要校验| Verify["添加 verifier 节点"]
    Verify --> TM
    Eval -->|继续| Strat
    Eval -->|完成| Report["聚合结果 → result.md → 回报请求方"]
    Eval -->|阻塞| Ask["询问请求方 / 上报阻塞"]

    style Strat fill:#ffe6e6,stroke:#cc3333
    style PM fill:#e6f2ff,stroke:#3366cc
    style TM fill:#e6ffe6,stroke:#339933
```

**配套技能**（Team Leader 工作区 `skills/`）：

| 技能 | 用途 |
|------|------|
| `team-coordination` | 协调策略层：DAG vs Loop、质量门禁、并行边界 |
| `project-management` | Project 状态、DAG/Loop 执行计划、ready-node 解析 |
| `task-management` | 子任务委派、结果检查、阻塞/修订处理 |
| `file-sharing` | 读写 `shared/`（团队可见）与 `global-shared/`（Manager 输入） |
| `organization` | 团队拓扑、Matrix 身份、房间查询 |
| `communication` | 消息纪律（何时 @mention、回报、NO_REPLY） |

## 5. 协调规则要点

1. **绝不绕过 Leader** — 与团队 Worker 的所有沟通都经由 Leader。
2. **一次委派一个任务** — 不同时给同一 Leader 派多个无关任务。
3. **信任 Leader 的分解** — Manager 不干预子任务分配与模式选择。
4. **升级路径** — Leader 报 BLOCKED 时，Manager 升级给 Admin。
5. **@mention 纪律** — 仅任务完成 / 阻塞 / 需要决策时 @mention 请求方，避免回声循环。
6. **Team Admin 任务独立** — Manager 不追踪、不干涉 Team Admin 发起（Leader DM）的任务。
