# Matrix 通道策略（允许列表）科普文：谁在什么房间里能跟谁说话

> 配套流程图：`controller-reconcile-flow.drawio` 中的 `channel-policy-flow` 与 `openclaw-allowlist-write-path` 两个页面。
> 对应源码：`agentteams-controller/internal/controller/team_controller.go`、`internal/service/deployer.go`、`internal/agentconfig/generator.go`

## 1. 这是什么

AgentTeams 里所有 Agent（Manager、Leader、Worker）都跑在 Matrix 上，靠房间沟通。既然大家共用同一套 Matrix 服务器，就必须回答一个安全问题：

> **谁的私聊（DM）我能回？谁在群聊里发的消息我能响应？**

答案是每个 Agent 的 `openclaw.json` 里那张「允许列表」。只允许列表上的人进入对话，其他人都被忽略——这就是 Matrix 通道的 **allowlist 策略**。本文讲清楚这张表是怎么算出来的、写进了哪里、由谁来维护。

## 2. 策略长什么样

每个 Agent 的配置 `agents/<worker>/openclaw.json` 里，`channels.matrix` 下有这些字段：

```jsonc
{
  "channels": {
    "matrix": {
      "groupPolicy": "allowlist",          // 群聊策略：白名单
      "groupAllowFrom": ["@leader:domain"], // 群聊里允许响应谁的 @ 提及
      "dm": {
        "policy": "allowlist",              // 私聊策略：白名单
        "allowFrom": ["@leader:domain"]     // 允许谁给我发私聊
      }
    }
  }
}
```

核心就两个数组：

| 字段 | 含义 |
|---|---|
| `groupAllowFrom` | 群聊中被允许向该 Agent 发消息的人（通常要带 `@提及`，见 `groups.*.requireMention`） |
| `dm.allowFrom` | 允许直接给该 Agent 发私聊的人 |

## 3. 默认值：谁都有资格

`buildMatrixChannelConfig`（`generator.go:227`）在生成基础配置时给出两套默认：

- **独立 Worker**：`groupAllowFrom` / `dm.allowFrom` 都是 `[@manager, @admin]`——只跟 Manager 和系统管理员说话。
- **Team Worker**（`req.TeamLeaderName` 非空）：变成 `[@leader, @admin]`——只跟自己的 Leader 和系统管理员说话。

注意：这两套只是「基础层默认」。Worker 一旦加入 Team，TeamReconciler 会用团队语义**覆盖**它（见第 5 节）。

## 4. 团队语义：Leader 和 Worker 区别对待

`teamChannelPolicy`（`team_controller.go:863`）是计算核心。输入是 Team + 全体成员 + 当前成员 + 角色，输出两份最终列表。规则：

**Team Leader（角色的 `team_leader`）**：
- 群聊允许：`Manager + 系统管理员 + 所有协调员（coordinator）+ 全部其他成员`。
- 私聊允许：`Manager + 系统管理员 + 所有协调员`。
- 一句话：Leader 是团队枢纽，Manager 可以找它，所有成员都能在群里找它，协调员可以私聊它。

**Team Worker（角色的 `team_worker`）**：
- 群聊允许：`Leader + 系统管理员 + 所有协调员`；如果 `spec.peerMentions` 未关闭（默认开启），再加**其余成员**（兄弟 Worker）。
- 私聊允许：`Leader + 系统管理员 + 所有协调员`。
- 一句话：Worker 主要听 Leader 指挥；协调员（人类）可以越级私聊或群聊直接找它；`PeerMentions` 控制同组 Worker 能否互相 @。

几个恒等式：
- **系统管理员永远在列表里**——操作者保持可见性（`team_controller.go:881`）。
- **协调员** = Team 的 `teamAdminMatrixID` + `spec.humanMembers` 里角色为空或为 `coordinator` 的人类成员（`teamCoordinatorIDs`，`team_controller.go:1298`）。
- 名字在代码里都是 `localpart`，统一通过 `resolve` 转成真实 MatrixID（`@name:domain`）。

## 5. 计算与合并：merge 之后再 apply

算出基础列表后，还有两层用户自定义叠加：

```
团队级 ChannelPolicy (t.Spec.ChannelPolicy)
        └─ mergeChannelPolicy ──► 合并 个人级 ChannelPolicy (worker.Spec.ChannelPolicy)
                                          │ 各自含 GroupAllowExtra / GroupDenyExtra /
                                          │      DmAllowExtra / DmDenyExtra
                                          ▼
groupAllow = applyChannelAllowPolicy(base, allowExtra, denyExtra, resolve)
dmAllow    = applyChannelAllowPolicy(base, dmAllowExtra, dmDenyExtra, resolve)
                                          │
                                          ▼
uniqueTeamStrings 去重、去空 ──► teamChannelAllowLists
```

- `mergeChannelPolicy`（`team_controller.go:1401`）：先取团队策略，再并入个人策略（团队级优先，个人级追加）。
- `applyChannelAllowPolicy`（`team_controller.go:940`）：在基础列表上先 `+allowExtra`，再 `-denyExtra`。注意 `DenyExtra` 只是**从允许列表里剔除**，不是 Matrix 层的硬屏蔽。
- `uniqueTeamStrings`：去空、去重，保证结果干净。

## 6. 写进哪里：两条写入路径

最终列表只落在一个对象上：MinIO 的 `agents/<worker>/openclaw.json`。写入有两条路径，必须保持一致。

**路径 A：WorkerReconciler 生成基础配置**（`ReconcileMemberConfig` → `Deployer.DeployWorkerConfig`）
- `GenerateOpenClawConfig` 按第 3 节的默认值生成（独立语义或 leader 语义）。
- `mergeUserPluginConfig` → `preserveChannelMatrixAllowFrom`（`deployer.go:1454`）：**如果旧文件里已有非空的 `groupAllowFrom` / `dm.allowFrom`，原样拷回生成结果**。
- 目的：Worker 每次重调和都会重新生成配置，如果没有这一层保护，团队注入的列表会被冲回独立默认值。

**路径 B：TeamReconciler 注入团队策略**（`teamChannelPolicy` → `Deployer.InjectChannelPolicy`，`deployer.go:707`）
- 从 MinIO 读现有 `openclaw.json`，用最终列表**原地 patch** `channels.matrix.groupAllowFrom` 与 `channels.matrix.dm.allowFrom`，再写回（`agentconfig.InjectChannelPolicy`，`generator.go:553`）。
- 空列表视为 no-op（不写），避免在信息不全时清掉有效策略。

两条路径共享同一个对象，靠「A 生成时保留、B 注入后由 Team 周期调和 + Worker status watch 重触发」形成闭环。

## 7. 删除 / 脱离：如何回退

Worker 离开团队或 Team 被删除时，`detachTeamMember`（`team_controller.go:792`）做完整回退：

1. 撤销协作上下文（`DropTeamContext: true`）。
2. 把 Manager 重新邀请回该 Worker 的个人房间。
3. **撤销** Manager 对它的 `groupAllowFrom`（普通 Worker 在团队期被移出 Manager 群聊）。
4. 重新注入独立语义列表：`[manager, 系统管理员, 团队管理员]`（`team_controller.go:852`）。

关键设计：**Team 不销毁 Worker**。回退只是把 Worker 恢复成「独立 Worker」，它仍然活着、仍然受 WorkerReconciler 管理。

## 8. 边界情况

- **QwenPaw 运行时**：不走 `openclaw.json`（用 `runtime/runtime.yaml`），`teamChannelPolicy` 直接跳过策略注入（`team_controller.go` 中的 runtime 分支）；`detachTeamMember` 也不更新 Manager 的 `groupAllowFrom`（`team_controller.go:842`）。
- **Manager 侧是同步动作**：`UpdateManagerGroupAllowFrom(leader=true, worker=false)` 双向调整 Manager 自己的群聊允许列表——Leader 被加进来，普通 Worker 被移出去。
- **CoPaw / Edge**：走 `runtime/runtime.yaml`，同样不涉及 `openclaw.json` 策略。

## 9. 一张图记住它

```
生成(基础默认: [manager,admin] 或 [leader,admin])
   │ 路径A  WorkerReconciler  ──preserveChannelMatrixAllowFrom 保护──┐
   ▼                                                               │
agents/<worker>/openclaw.json  channels.matrix.groupAllowFrom       │
        ▲                        channels.matrix.dm.allowFrom       │
   │ 路径B  TeamReconciler  ──teamChannelPolicy + InjectChannelPolicy│
   └──────────── 周期调和 / Worker status watch 重触发 ────────────┘
```

## 10. Q&A

**Q：Worker 被团队接管后，个人 `spec.channelPolicy` 还有效吗？**
A：有效。它作为个人级策略经 `mergeChannelPolicy` 合并进最终列表（第 5 节）。

**Q：`denyExtra` 能把系统管理员拉黑吗？**
A：理论上 apply 会剔除，但 `teamChannelPolicy` 的基础列表里系统管理员是恒在的，且 `InjectChannelPolicy` 对空列表是 no-op，实际以最终写入值为准。

**Q：为什么 Worker 侧重生成 openclaw.json 不会把团队策略冲掉？**
A：`preserveChannelMatrixAllowFrom` 会把旧文件里的团队列表拷回新生成的配置（第 6 节路径 A）。这是团队策略能稳定存在的前提。
