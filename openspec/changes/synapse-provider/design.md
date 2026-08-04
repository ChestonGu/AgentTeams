# Synapse Provider Support Design

> Change: `synapse-provider`
> 状态：Design
> 前置：`proposal.md`、delta spec `specs/matrix-provider/spec.md`
> 对应愿景：`docs/design/synapse-support-vision.md`（§3-§5）
> 吸收来源：`openspec/changes/support-synapse-homeserver`（D1-D7 决策、启动引导、迁移计划）

## Context

现状：`Client` 接口（`internal/matrix/client.go:30`）唯一实现为 `TuwunelClient`，全部管理操作经
Tuwunel admin bot（`#admins` 房间 `!admin` 命令）完成；worker 房间由 admin token 兜底创建，
admin 驻留每个房间；`app.go:288` 无条件实例化 Tuwunel。Helm 的 `values.yaml` 已预留
`matrix.provider: tuwunel | synapse` 注释与空 `matrix.synapse: {}` 块，但 `00-validate.yaml`
拒绝除 tuwunel 外的任何值。`internal/service/interfaces.go:57-59` 记录着"高 power 用户普通 kick
踢不掉、需 ForceLeaveRoom"的限制。

目标：provider 无关 + Synapse 第一公民（端到端：Helm/controller/Manager/安装器）；admin 永不进房；
房间内操作走创建者身份，服务端操作走 Admin API；用户注册保持 registration_token 零分支。

## Goals

- `matrix.provider=synapse` + `matrix.mode=managed` 在 Helm 上端到端安装成功（生产路径）。
- 全局 admin 不在任何 AgentTeams 房间，全生命周期操作仍成功。
- `SynapseClient` 实现同一 `matrix.Client` 接口，由 `AGENTTEAMS_MATRIX_PROVIDER` 选择；
  每个 Tuwunel admin-bot 操作都有 Synapse admin-API 等价实现（除 AdminCommand 显式报错外）。
- AS 注册为部署期静态配置（Helm 渲染 + 挂载）；controller 启动 fail-fast 校验，无状态机。
- Manager 启动引导、嵌入式镜像、安装器、镜像同步、文档与技能全部按提供商感知。
- Tuwunel 保持默认；现有 Tuwunel 部署零行为变化。

## Non-Goals

- 不删除 `TuwunelClient`；不做动态 AS 注册；不引入第三方 Matrix SDK；不做"任意身份发消息"端点。
- `matrix.mode=existing` + 外部 Synapse（只支持 managed）；Tuwunel→Synapse 在线迁移；Synapse Postgres 后端。

## Decisions

### D1: 按提供商选择的客户端工厂，而不是第二个接口

controller `Config` 新增 `MatrixProvider`（来自 `AGENTTEAMS_MATRIX_PROVIDER`，默认 `tuwunel`，
未知值启动即失败）。引入 `matrix.NewClient(provider string, cfg Config, http *http.Client) Client`，
返回 `*TuwunelClient` 或 `*SynapseClient`。保留 `NewTuwunelClient` 作为现有约 25 个调用点
（多数是测试）的薄包装，把生产构建点（app 引导 / initializer）迁移到工厂。

理由：接口本来就提供商无关，只需两个实现。备选方案（配置内放提供商接口、单独 AdminClient 抽象）均否决。

### D2: SynapseClient 复用 CS-API 基础设施，只有 admin 路径不同

`SynapseClient` 与 `TuwunelClient` 共享 HTTP/JSON 基础设施（`doJSON`、令牌缓存、`UserID`、
`ResolveRoomAlias`、`CreateRoom`、`SetRoomState` 等）——通过公共基类或共享辅助函数。提供商特有成员：

| 操作 | Tuwunel（现状） | Synapse |
|---|---|---|
| 孤儿恢复 / `SetPasswordAsAdmin` | `!admin users reset-password` | `PUT /_synapse/admin/v2/users/{user_id}`（password/logout_devices/deactivated） |
| 停用（`DeactivateHumanUser`） | `!admin users deactivate` | `POST /_synapse/admin/v1/deactivate/{user_id}`（`erase: false`）——**SSO Human 删除依赖（externalsso/source.go:108），必须保留** |
| `RegisterAppService` / `UnregisterAppService` | `!admin appservices register/unregister` | 返回 nil（Helm 部署期渲染注册，见 D3） |
| `AppServiceSmokeTest` | AS 登录（`m.login.application_service` + as_token） | 相同（两者都可用），语义改为"校验部署正确性" |
| `AdminCommand` | 向 `#admins:<domain>` 发送 `!admin ...` | 返回明确"Synapse 不支持 admin bot"错误 |
| Agent 删除时的房间清理 | controller leave/forget + `CONDUWUIT_DELETE_ROOMS_AFTER_LEAVE` 折叠 | controller leave + `DELETE /_synapse/admin/v2/rooms/{room_id}`（purge）+ `delete_status` 轮询 |

理由：reset-password / deactivate / AS 冒烟是 controller 生产路径实际使用的 admin 操作；映射到
Synapse admin API 在零接口改动下保持功能对等。`DeactivateHumanUser` 虽是 1.2.0 的 SSO 依赖，仍保留映射。

### D3: Synapse 上的 AppService 注册 = Helm 部署期文件挂载（非 pre-install job）

Helm 把 AppService 注册 YAML（与 `RenderAppServiceRegistration` 同结构，as_token/hs_token 来自
runtime-env Secret——与 Tuwunel **共用同一 Secret 源**，天然一致）渲染进 ConfigMap，挂载到
Synapse 容器固定路径（如 `/data/synapse/appservices/agentteams-controller.yaml`），生成的
`homeserver.yaml` 在 `app_service_config_files` 下列出它。controller 在
`AGENTTEAMS_MATRIX_PROVIDER=synapse` 时跳过 `RegisterAppService`/`UnregisterAppService`，只跑
`AppServiceSmokeTest`（fail-fast，见 P0 落点）。Tuwunel 保留运行时 admin-bot 注册（现状）。

**相比 pre-install job 的改进**：Helm 模板从同一 Secret 双渲染（AS YAML + homeserver.yaml）
天然保证 as_token 一致，无需额外的部署期 job；`EnsureTokens()` 只负责首次生成并写入 Secret。

嵌入式模式：`start-synapse.sh` 从环境变量渲染 homeserver.yaml 与 AS 注册 YAML（AS 令牌由安装器生成传入）。

### D4: Synapse 启动引导解决注册鸡生蛋问题（吸收）

Synapse 的 `m.login.registration_token` 流程只接受预创建的令牌，而全新服务器没有现成 admin 用户。
chart 的 Synapse 容器（及嵌入式 `start-synapse.sh`）在 exec 启动 homeserver 前执行 `bootstrap-synapse.sh`：

1. 数据卷生成/保留签名密钥（`homeserver.signing.key`）。
2. admin 用户不存在时用 `register_new_matrix_user -c homeserver.yaml -a -u $ADMIN_USER -p $ADMIN_PASSWORD http://localhost:8008` 创建（经 `registration_shared_secret` 认证）。
3. `POST /_synapse/admin/v1/registration_tokens/new {"token": "<AGENTTEAMS_REGISTRATION_TOKEN>"}` 创建注册令牌（幂等，已存在则跳过），使用登录获得的 admin 令牌。
4. `exec python -m synapse.app.homeserver -c homeserver.yaml`。

生成的 `homeserver.yaml`：`server_name`、8008 `client` 监听器（显式启用时加 8448 联邦监听器）、
`enable_registration: true`、`registration_shared_secret`（来自 `matrix.synapse.registrationSharedSecret` 值/自动生成）、
`registration_requires_token: true`、媒体存储 `/data`、`app_service_config_files`。

理由：让 controller/Manager 在两个提供商上的注册调用保持**逐字节一致**（`m.login.registration_token`），
`EnsureUser`/Manager 注册代码路径**不分支**。备选（`m.login.shared_secret`、开放注册）均否决——
前者破坏现有分布式 `AGENTTEAMS_REGISTRATION_TOKEN` 契约，后者不安全。

### D5: 提供商感知的 Manager 启动引导与路由（吸收）

- 就绪检查：`start-manager-agent.sh` 对**两个**提供商都探测 `GET /_matrix/client/versions`；
  `/_tuwunel/server_version` 仅在 `AGENTTEAMS_MATRIX_PROVIDER=tuwunel` 时作为附加检查保留。
  `install/agentteams-install.sh` 的 `wait_matrix_ready` 与 `tests/` 辅助函数同样切换。
- Higress（k8s 模式）：service-source 名称与 `/_matrix` 路由目标由 `AGENTTEAMS_MATRIX_PROVIDER`
  决定（`tuwunel` → `tuwunel.dns`，`synapse` → `synapse.dns`）。controller 通过
  `ManagerAgentEnv`/`WorkerEnvDefaults` 把 `AGENTTEAMS_MATRIX_PROVIDER` 传播到 Manager/Worker。
- 嵌入式：`manager/supervisord.conf` 把 `[program:tuwunel]` 换成单个 `[program:matrix]` 分发器
  （`start-matrix.sh`），按 `AGENTTEAMS_MATRIX_PROVIDER` exec `start-tuwunel.sh` 或 `start-synapse.sh`。
  Synapse 监听 8008，Tuwunel 监听 6167；controller 的 `AGENTTEAMS_MATRIX_URL` 已来自安装期环境变量。

### D6: Synapse 镜像与 values（吸收）

`matrix.synapse` values 镜像 `matrix.tuwunel` 形状：`image.repository/tag/pullPolicy`
（默认同步镜像 `higress-registry.cn-hangzhou.cr.aliyuncs.com/higress/synapse`，固定发布 tag；
**注意固定 tag 不得为 `v1.127.0`**——该版本含 CVE-2025-30355（高危联邦漏洞，已在野外被利用），
最小安全版本为 `v1.127.1`，见 REQ-MP-13/镜像同步任务）、
`replicaCount`（1）、`resources`、`service.type/port`（8008）、`persistence`（size/class/mountPath `/data`）、
`registrationSharedSecret`（空则自动生成进 runtime Secret）、`extraEnv`。
`hack/mirror-images.sh` 与嵌入式 controller 镜像清单新增同步条目。Element Web、Higress 路由与所有
`/_matrix` 消费者提供商无关。

### D7: 文档与技能（吸收）

- README/quickstart/architecture：记录 `matrix.provider=synapse`、`matrix.synapse` values 块、
  资源占用说明、切换提供商需全新安装。
- `matrix-server-management` 技能：新增提供商感知小节——Tuwunel admin-bot 命令 vs Synapse admin API
  （`/_synapse/admin/v1/...`），由 `AGENTTEAMS_MATRIX_PROVIDER` 驱动；绝不指示对 Synapse 使用 `!admin ...`。
- `changelog/current.md`：每个逻辑变更一条（镜像内容政策）。

## Architecture

### 身份职责分离

```
┌──────────────────────────────┐        ┌──────────────────────────────┐
│  服务端操作（房间外）          │        │  房间内操作                   │
│  Admin API（/_synapse/admin） │        │  AS 代登录房间创建者           │
│  - 用户生命周期               │        │  - invite / kick              │
│  - 房间删除（purge + 轮询）   │        │  - 发消息 / 改房间名          │
│  - 成员列表 / AS 静态注册     │        │  - 探测创建者（creator 字段）  │
└──────────────────────────────┘        └──────────────────────────────┘
              ▲                                          ▲
              │  admin 凭据（永不进房）                    │  as_token（无密码）
              └──────────────┬───────────────────────────┘
                             │
                    ┌────────┴────────┐
                    │  SynapseClient  │
                    │  (新实现)        │
                    └─────────────────┘
```

### 运行时分支（app.go）

```go
// internal/app/app.go（改造）
func newMatrixClient(cfg config) matrix.Client {
    switch cfg.MatrixProvider {
    case "synapse":
        sc := matrix.NewSynapseClient(cfg.Synapse)   // AdminOps + C-S + AS
        if err := sc.VerifyAppService(); err != nil { // 启动 fail-fast（REQ-MP-11）
            return nil, fmt.Errorf("as 一致性校验失败: %w ...", err)
        }
        return sc, nil
    default: // tuwunel，保持现状
        return matrix.NewTuwunelClient(cfg.Tuwunel), nil
    }
}
```

## Interface Contracts

### `SynapseClient`（`internal/matrix/synapse_client.go`，新文件）

```go
type SynapseClient struct {
    baseURL    string // https://<homeserver>
    adminToken string // 全局 admin 访问 token（服务端操作，永不进房）
    asToken    string // AS as_token（与 homeserver.yaml 一致，来自同一 Secret）
    hsToken    string // AS hs_token
    http       *http.Client
}

// AdminOps 子接口：Synapse 特有端点，Tuwunel 路径不实现（保持零回归）
// 注意：用户注册 NOT 在此——注册走 m.login.registration_token（REQ-MP-12，与 Tuwunel 零分支）
// 注意：invite/kick NOT 在此——Synapse 无 admin invite/kick 端点（/rooms/{id}/members 仅有 GET），
//       房间内成员操作以创建者身份走 C-S API（REQ-MP-9 + RoomCreator）
type AdminOps interface {
    // —— 用户 ——
    GetUser(ctx context.Context, userID string) (*SynapseUser, error)         // GET  /_synapse/admin/v2/users/{id}
    UpdateUser(ctx context.Context, userID string, u UserUpdate) error        // PUT  /_synapse/admin/v2/users/{id}（password/logout_devices/deactivated）
    DeactivateUser(ctx context.Context, userID string, erase bool) error      // POST /_synapse/admin/v1/deactivate/{id}
    // —— 房间 ——
    GetRoom(ctx context.Context, roomID string) (*SynapseRoom, error)         // GET  /_synapse/admin/v1/rooms/{id}（含顶层 creator 字段）
    DeleteRoom(ctx context.Context, roomID string, purge bool) (string, error) // DELETE /_synapse/admin/v2/rooms/{id}（异步 → 返回 delete_id）
    GetDeleteStatus(ctx context.Context, deleteID string) (string, error) // GET  /_synapse/admin/v2/rooms/delete_status/{deleteID}（TaskStatus 枚举：scheduled|active|complete|failed；响应仅 delete_id/status/shutdown_room，无 error 字段）
    ListRoomMembers(ctx context.Context, roomID string) ([]string, error)     // GET  /_synapse/admin/v1/rooms/{id}/members
}
```

### `RoomCreator`（`internal/matrix/room_creator.go`，新文件）

```go
type RoomCreator struct {
    admin   AdminOps          // 创建者探测
    client  *SynapseClient    // AS 代登录
    cache   sync.Map          // roomID → creatorUserID
}

// 探测创建者：优先 Room Details API 顶层 creator 字段（REQ-MP-9）；兜底 alias 解析 + 最高 power 成员
func (r *RoomCreator) ResolveCreator(ctx, roomID) (string, error)

// 以创建者身份执行：AS 代登录创建者（无密码）→ fn(token)
func (r *RoomCreator) ActAsCreator(ctx, roomID string, fn func(accessToken string) error) error

// 房间内成员操作（invite/kick）经 ActAsCreator 以创建者身份走 C-S API（REQ-MP-10）：
//   InviteToRoom   → POST /_matrix/client/v3/rooms/{roomID}/invite（创建者身份，AS 代登录）
//                    + POST /_matrix/client/v3/rooms/{roomID}/join（controller 以目标身份：目标自有
//                      token，或 AS 代登录无密码）——两步合一：邀请后目标自动进房，无需手动接受
//   ForceLeaveRoom → POST /_matrix/client/v3/rooms/{roomID}/kick
// （Synapse 无 admin invite/kick 端点；成员列表仍用 AdminOps.ListRoomMembers）
// 注意：Matrix invite 语义默认仅置 membership=invite，目标必须 join 才进房——controller 代为 join
//       实现"邀请后自动进房"（与 Tuwunel 路径 provisioner.go:492 的 JoinRoom 模式一致）
```

### 错误语义

| 错误 | 语义 | 处理 |
|---|---|---|
| `M_UNKNOWN_TOKEN`（AS 校验） | as_token 漂移 | fail-fast 退出，提示检查 Secret 与 homeserver.yaml（REQ-MP-11） |
| `M_NOT_FOUND`（Admin API 404） | 用户/房间不存在 | 按调用方语义返回（EnsureUser → 走注册分支） |
| `M_LIMIT_EXCEEDED`（429） | 限流 | 指数退避重试（上限 5 次） |
| Admin API 非 2xx | 服务端拒绝 | 包装为 `AdminAPIError{Status, Endpoint, Body}`，含端点上下文 |

### 幂等性

- 房间内成员操作（invite/join/kick，创建者身份 + 目标身份 C-S API）：invite 目标已在房间、join 目标已为 join 态、kick 目标不在房间时视为成功（幂等，按 C-S API 错误码处理）。
- `UpdateUser`/`DeactivateUser`：重复调用幂等。
- `DeleteRoom`：同一 delete_id 轮询幂等（`GET delete_status/{delete_id}`，按 status=complete 终止）；已完成后再删返回新的 delete_id（task 保留 7 天后被清理，此后轮询 404）。
- bootstrap 引导（D4）：admin 存在则跳过、注册令牌存在则跳过（幂等）。

## P0 决策落点：AS 静态注册（二态模型）

Synapse 的 AS 是**静态注册**（homeserver.yaml + 重启加载），与 Tuwunel 动态 `!admin appservices register` 本质不同：

- **注册一次，永不更新**；as_token 是静态凭证，配置不变就永远有效、不会失效。
- **失效只来自 token 漂移**：Secret 未持久化导致重建后 `EnsureTokens()` 自动生成新 token、显式轮换、Synapse 侧配置被改。
- **二态模型，无状态机**：一致 → 永远工作；不一致 → 启动即失败。不需要"轮询等待注册完成"。

启动流程（替换 Tuwunel 的 RegisterAppService 语义）：

```
controller 启动
  -> AppServiceSmokeTest(as_token)   // 复用 appservice.go:109，语义改为"校验部署正确性"
  -> 通过  -> 继续（之后永不重试、永不失效）
  -> 失败  -> fail-fast 明确报错并退出（code != 0，提示检查 as_token 一致性）
```

一致性保障（替换 pre-install job 方案）：Helm 模板从**同一 runtime Secret** 双渲染
（AS 注册 YAML + homeserver.yaml），天然一致；`EnsureTokens()` 仅首次生成并写入 Secret。

轮换 = 显式双端一致运维操作（更新 Secret → 重新渲染 chart → 重启 Synapse 与 controller），写入运维文档。

## Migration Plan

1. **全新安装**：`--set matrix.provider=synapse`（外加可选 `matrix.synapse.*` 覆盖）产出 Synapse 支撑的整套栈；`--set matrix.provider=tuwunel`（默认）不变。
2. **升级现有 Tuwunel 安装**：纯增量——新 values 默认值保持现有行为；controller 从 chart 拿到 `AGENTTEAMS_MATRIX_PROVIDER=tuwunel`。无需迁移步骤。
3. **在线上部署中切换提供商**：不支持原地切换（数据存储不同）。文档化流程：备份工作区，用另一个提供商全新安装，重新跑工作流。
4. **回滚**：还原 `matrix.provider` 与 `matrix.synapse.*` values；下次 `helm upgrade` 重新渲染 Tuwunel 资源。controller 镜像变更随同一版本发布，values 与上一镜像 tag 成对回滚。
5. **嵌入式/本地**：安装器新增提供商选择（默认 `tuwunel`）；`install uninstall` 路径不变。

## 文件改动映射

| 文件 | 改动 |
|---|---|
| `internal/matrix/client.go` | 新增 `SynapseClient`；`CreateRoom` 去除 admin token 兜底（:485）；`SyncMessages` 移出接口 |
| `internal/matrix/synapse_client.go` | **新文件**：`SynapseClient` + `AdminOps`（上表契约） |
| `internal/matrix/room_creator.go` | **新文件**：`RoomCreator`（探测 + 缓存 + ActAsCreator） |
| `internal/matrix/appservice.go` | `RegisterAppService`/`UnregisterAppService` 在 Synapse 路径返回 nil；smoke test fail-fast |
| `internal/matrix/appservice_config.go` | `EnsureTokens()` 语义收紧：仅首次初始化生成并写入 Secret |
| `internal/config/config.go` | 新增 `MatrixProvider`（`AGENTTEAMS_MATRIX_PROVIDER`，默认 tuwunel，无效值 panic） |
| `internal/app/app.go` | `NewTuwunelClient`（:288）改为工厂 `NewClient`（保留薄包装） |
| `internal/service/provisioner.go` | worker 房间创建改为 AS 创建者 token；:412 去 admin invite；房间清理走 Admin API |
| `internal/service/provisioner_human.go` | `ForceLeaveRoom` 以创建者身份走 C-S API kick；`DeactivateHumanUser` 走 Admin API |
| `helm/agentteams/values.yaml` | `matrix.synapse.*` 完整块；provider 枚举补全 |
| `helm/agentteams/templates/00-validate.yaml` | 放行 `provider=synapse`（managed） |
| `helm/agentteams/templates/matrix/` | **新**：`synapse-statefulset.yaml`、`synapse-service.yaml`、`synapse-homeserver-config.yaml`、`synapse-appservice-registration.yaml`、`synapse-bootstrap.yaml` |
| `helm/agentteams/templates/controller/deployment.yaml` | 新增 `AGENTTEAMS_MATRIX_PROVIDER` 环境变量 |
| `helm/agentteams/templates/_helpers.infra.tpl` | `matrix.internalURL`/`serverName` 按提供商分支 |
| `manager/scripts/init/` | **新**：`start-synapse.sh`、`start-matrix.sh`；修改 `start-manager-agent.sh`（就绪检查/Higress 分支） |
| `manager/supervisord.conf` | `[program:tuwunel]` → `[program:matrix]` 分发器 |
| `install/agentteams-install.sh`（及 `.ps1`） | 提供商选择 + 健康检查按提供商感知 |
| `hack/mirror-images.sh` | Synapse 镜像同步 |
| `manager/agent/skills/matrix-server-management/` | 提供商感知小节 |
| `test/testutil/mocks/` | `SyncMessages` 移除；新增 AdminOps mock |
| `tests/lib/`、`docs/` | 探测切换 `/_matrix/client/versions`；README/quickstart/architecture 更新 |

## 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| Synapse Admin API 版本差异 | 端点字段漂移 | 锁定 Synapse 版本（≥ v1.127.1，CVE-2025-30355 修复版）；集成测试固化 |
| admin invite/kick 端点不存在 | 房间内成员操作需房间内身份 | 创建者身份 C-S API（REQ-MP-10）；创建者 power=100 可踢任意普通成员 |
| AS namespace 独占（`@.*:domain`） | 共享 homeserver 越权 | 仅 AgentTeams 独占 homeserver；`APP_SERVICE_USER_NAMESPACE_REGEX` 收紧 |
| 房间删除异步语义差异 | 删除时序不确定 | 封装 `delete_status` 轮询；purge 参数对齐 |
| 创建者探测失败 | 无法确定操作身份 | 兜底：alias 解析 + 最高 power 成员（REQ-MP-9 场景） |
| as_token 漂移（Secret 未持久化） | 启动即失败（`M_UNKNOWN_TOKEN`） | Helm 同一 Secret 双渲染；fail-fast 报错；`EnsureTokens()` 仅首次生效 |
| 注册令牌引导失败 | homeserver 无法启动 | 引导幂等 + 容器重启策略重试；admin/令牌未就绪前不启动 homeserver |
| Synapse 比 Tuwunel 重 | 资源占用高 | Synapse 保持可选；Tuwunel 仍默认；`matrix.synapse.resources` 记录推荐值 |
| `AdminCommand` 在 Synapse 上返回错误 | 未来直接调用方失败 | 所有 admin-bot 用法限制在两个已映射到 Admin API 的接口方法内；单测断言 Synapse 上直接调用 `AdminCommand` 报错 |
| 双提供商房间清理行为漂移 | 清理不彻底 | 清理保持 controller 驱动（提供商无关 leave/forget）；Synapse 追加 admin-API purge/delete |
| 迁移期双路径回归 | Tuwunel 部署受影响 | `TuwunelClient` 不删不改；provider 分支隔离；CI 双跑 |

## Testing

- **单元（httptest mock Synapse）**：AdminOps 各端点请求/响应/幂等/429 退避；RoomCreator 探测与兜底；
  registration-token 注册、M_USER_IN_USE 登录回退、孤儿恢复；直接 `AdminCommand` 报错断言。
- **集成（真实 Synapse 容器）**：全生命周期 worker/team/human（创建、加房、消息、删除、停用）；
  admin 不在任何房间断言；bootstrap 引导幂等。
- **双跑**：Tuwunel 路径 CI 全绿（无回归）；`helm template` 双提供商渲染检查。
- **启动时序**：as_token 漂移场景 → 控制器 fail-fast 退出（REQ-MP-11 场景）。
