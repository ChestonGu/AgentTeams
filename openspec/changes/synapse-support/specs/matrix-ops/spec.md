## Purpose

Provide a single, business-capability-oriented abstraction over Matrix homeserver operations so the controller's business layer (Provisioner, Initializer, HTTP handlers) is fully decoupled from Matrix protocol details (token selection, API paths, admin-bot commands, provider-specific fallbacks). This lets the controller swap the underlying homeserver (Tuwunel ↔ Synapse) via an env var with zero changes to business orchestration code.

The capability covers six business domains: User Identity & Credentials, Room Lifecycle, Room Membership, Room Metadata & Messaging, AppService Governance, and Queries. It replaces direct use of the `matrix.Client` protocol-layer interface throughout the business layer.

## ADDED Requirements

### Requirement: MatrixOps interface SHALL be the only Matrix surface the business layer depends on

The `internal/matrix` package SHALL export a single `MatrixOps` interface covering all Matrix-related operations the business layer needs. `Provisioner`, `Initializer`, and HTTP handlers SHALL depend on `MatrixOps` and SHALL NOT reference `matrix.Client`, `matrix.TuwunelClient`, `matrix.SynapseClient`, or any `!admin` command string. Existing protocol-layer types (`matrix.Client`, `TuwunelClient`, `SynapseClient`) SHALL be retained as internal HTTP clients used by MatrixOps implementations but no longer appear in business-layer signatures.

#### Scenario: Provisioner holds MatrixOps, not Client

- **WHEN** `NewProvisioner` is constructed
- **THEN** its Matrix dependency is typed `matrix.MatrixOps` (field `matrixOps`), and no field of type `matrix.Client` exists on the struct

#### Scenario: No admin-bot command string in business layer

- **WHEN** the `internal/service/` and `internal/server/` and `internal/initializer/` packages are grepped for `"!admin`
- **THEN** zero matches are returned (all such strings live inside `internal/matrix/*_ops.go`)

#### Scenario: appservice_mgmt_handler no longer hardcodes TuwunelClient

- **WHEN** `appservice_mgmt_handler.go::RotateToken` needs to rotate an AS token
- **THEN** it calls methods on an injected `MatrixOps` (or a small rotation helper interface) rather than constructing `matrix.NewTuwunelClient`

### Requirement: MatrixOps SHALL group methods by business capability, not protocol verb

MatrixOps SHALL expose methods named after business intent. Each method's signature SHALL accept business-typed specs/values (e.g., `RoomSpec`, `MemberSpec`, `RoomMetadata`) and return business refs (`RoomRef`, `UserRef`), NOT raw Matrix protocol fields (raw token strings, raw `state_key`, raw `m.room.member` JSON). Internal protocol details (which token to use, whether to retry, which admin API to call) SHALL be decided inside the implementation.

The interface SHALL cover at minimum the following six capability groups, with method counts roughly: User Identity & Credentials (~10), Room Lifecycle (~5), Room Membership (~8), Room Metadata & Messaging (~3), AppService Governance (~3), Queries & Ops (~3). The exhaustive method list is enumerated in scenarios below.

#### Scenario: Room creation uses RoomSpec, not protocol fields

- **WHEN** a caller invokes the room-creation method
- **THEN** the method signature accepts a `RoomSpec` (carrying name, topic, invitee user IDs, power-level overrides keyed by user ID, alias localpart, metadata, is_direct, e2ee flag) and returns a `RoomRef` (room_id + created bool), NOT a raw `CreateRoomRequest` exposing `CreatorToken`

#### Scenario: Member add abstracts inviter-token choice

- **WHEN** a caller invokes the add-member method
- **THEN** the signature accepts `roomID` and `userID` (and optionally a business-level "actor" hint), NOT a raw `inviterToken` — the implementation decides which token to use

#### Scenario: All six capability groups represented

- **WHEN** the MatrixOps interface is inspected
- **THEN** it contains methods covering: user provisioning/login/password/displayname, room create/dissolve/alias/archive, member add/remove/reconcile/join/leave/force-leave-all, metadata rename/set-state/send-message, appservice register/unregister/smoke-test, and queries list-members/list-joined/health-check

### Requirement: RoomSpec SHALL carry business intent, not protocol strategy

The `RoomSpec` passed to `CreateRoom` SHALL describe WHAT the room is (name, topic, invitees, per-user power levels, alias localpart, metadata, direct flag, e2ee flag) without prescribing HOW to create it (no `CreatorToken`, no API path, no preset constant). The implementation SHALL derive the creator identity, ensure the creator appears in `power_level_content_override.users` (required by Synapse 1.127 — see `synapse/handlers/room.py:878-888`), and choose the appropriate CS API call.

#### Scenario: Caller does not specify creator token

- **WHEN** `CreateRoom(ctx, RoomSpec{Name:..., Invite:..., PowerLevels:...})` is invoked
- **THEN** the spec type has no `CreatorToken` field; the implementation resolves the creator (admin by default) internally

#### Scenario: Creator auto-injected into power levels

- **WHEN** a caller provides `PowerLevels` that omits the resolved creator user ID
- **THEN** the implementation injects the creator with PL 100 before sending the request, so the call does not fail on Synapse with `M_BAD_JSON`

### Requirement: CreateRoom SHALL be idempotent on room alias

`CreateRoom` SHALL be idempotent when `RoomSpec.AliasLocalpart` is set: if a room with that alias already exists, the implementation SHALL resolve the alias to its room ID and return `RoomRef{RoomID: ..., Created: false}` rather than failing. This preserves the existing alias-based idempotency contract.

#### Scenario: Duplicate alias resolves to existing room

- **WHEN** `CreateRoom` is called with `AliasLocalpart="agentteams-worker-alice"` and the homeserver already has that alias
- **THEN** the call returns `RoomRef{RoomID: <existing>, Created: false}` without error

### Requirement: DissolveRoom SHALL best-effort delete the room without requiring operator in-room

`DissolveRoom(roomID)` SHALL remove the room from the homeserver on a best-effort, fire-and-forget basis. The caller SHALL NOT need to specify which user dissolves the room or be in the room. On Tuwunel the implementation issues `!admin rooms delete-room <roomID>` via the admin bot. On Synapse the implementation issues `DELETE /_synapse/admin/v2/rooms/<roomID>` and discards the returned `delete_id` (fire-and-forget, consistent with Tuwunel's async admin-bot semantics).

#### Scenario: DissolveRoom on Synapse uses admin v2 endpoint

- **WHEN** `DissolveRoom(ctx, "!room:dom")` is invoked on SynapseMatrixOps
- **THEN** the implementation issues `DELETE /_synapse/admin/v2/rooms/!room:dom` and returns nil on any 2xx response, without polling `delete_status`

#### Scenario: DissolveRoom on Tuwunel uses admin bot

- **WHEN** `DissolveRoom(ctx, "!room:dom")` is invoked on TuwunelMatrixOps
- **THEN** the implementation sends `"!admin rooms delete-room !room:dom"` to the admin bot room and returns nil once delivered

### Requirement: AddMember SHALL succeed when the operator is not yet in the room

When inviting a user would fail because the operator (the user whose token the implementation uses) is not joined to the room or lacks the power to invite, the implementation SHALL recover transparently. On Synapse, recovery SHALL classify the sender-side rejection (`classifySynapseSenderError`): a not-in-room error (`"... not in room ..."`, `event_auth.py:687`) triggers force-join via `POST /_synapse/admin/v1/join/{roomID}` (the native membership-restore endpoint: auto-invite + join), and an insufficient-power error (`"You don't have permission to invite users"`, `event_auth.py:703`) triggers `POST /_synapse/admin/v1/rooms/<roomID>/make_room_admin` (raises the sender to the room's highest admin power level); either recovery is followed by a retry of the CS API invite. On Tuwunel, recovery uses the admin bot's inherent cross-room privilege (no special step needed). The caller SHALL NOT see the recovery step — only the final success or failure.

#### Scenario: Synapse AddMember recovers via force-join when sender not in room

- **WHEN** `AddMember(ctx, roomID, userID)` runs on SynapseMatrixOps and the first CS invite attempt fails with `403 M_FORBIDDEN "@admin:dom not in room !room:dom."` (per `event_auth.py:687`)
- **THEN** the implementation issues `POST /_synapse/admin/v1/join/{roomID} {"user_id":"@admin:dom"}`, retries the CS invite, and returns nil on success

#### Scenario: Synapse AddMember recovers via make_room_admin when sender lacks power

- **WHEN** `AddMember(ctx, roomID, userID)` runs on SynapseMatrixOps and the first CS invite attempt fails with `403 M_FORBIDDEN "You don't have permission to invite users"` (per `event_auth.py:703`)
- **THEN** the implementation issues `POST /_synapse/admin/v1/rooms/<roomID>/make_room_admin {"user_id":"@admin:dom"}`, retries the CS invite, and returns nil on success

#### Scenario: Tuwunel AddMember relies on admin-bot privilege

- **WHEN** `AddMember(ctx, roomID, userID)` runs on TuwunelMatrixOps
- **THEN** the implementation issues a single CS invite using the admin token and returns nil on success — admin bot's cross-room privilege means no in-room requirement

#### Scenario: AddMember idempotent when target already joined

- **WHEN** `AddMember` runs and the homeserver returns `403 M_FORBIDDEN "@target:dom is already in the room."` (per `event_auth.py:697`)
- **THEN** the implementation returns nil (idempotent success) on both providers

### Requirement: RemoveMember SHALL treat only "target not in room" as idempotent and SHALL fall back when CS kick fails

When kicking a user, the implementation SHALL return nil (idempotent success) ONLY when the error indicates the target is not in the room (Synapse 1.127 actual string: `"The target user is not in the room"`, per `synapse/handlers/room_member.py:1022,1039`). It SHALL NOT treat sender-not-in-room errors (`"@sender not in room"` per `event_auth.py:687`) or sender-PL-insufficient errors (`"You cannot kick user"` per `event_auth.py:717`) as idempotent.

When the CS kick fails due to sender-not-in-room or sender-PL-insufficient, the implementation SHALL fall back using the same classified sender recovery as AddMember (force-join via `POST /_synapse/admin/v1/join/{roomID}` when the sender is not in the room, `make_room_admin` when the sender lacks power), then retry the CS kick. On Tuwunel, the fallback is `!admin users force-leave-room <userID> <roomID>` via the admin bot. The fallback's error-matching SHALL be case-insensitive and SHALL recognize Synapse's actual error strings (`"cannot kick user"`, `"cannot unban user"`, `"not in room"`) in addition to Tuwunel's `"not have enough power"`.

#### Scenario: Target already left → idempotent success

- **WHEN** `RemoveMember(ctx, roomID, userID, reason)` receives Synapse response `403 M_FORBIDDEN "The target user is not in the room"`
- **THEN** the call returns nil

#### Scenario: Sender not in room → fallback on Synapse

- **WHEN** `RemoveMember` receives Synapse response `403 M_FORBIDDEN "@admin:dom not in room !room:dom."`
- **THEN** the implementation force-joins the admin via `POST /_synapse/admin/v1/join/{roomID}`, retries the CS kick, and returns nil on success (NOT idempotent — the recovery step actually runs)

#### Scenario: Sender PL insufficient → fallback on Tuwunel

- **WHEN** `RemoveMember` receives Tuwunel-style error `"sender does not have enough power to kick target user"`
- **THEN** the implementation sends `"!admin users force-leave-room <userID> <roomID>"` to the admin bot

#### Scenario: Sender PL insufficient → fallback on Synapse

- **WHEN** `RemoveMember` receives Synapse response `403 M_FORBIDDEN "You cannot kick user @target:dom."`
- **THEN** the implementation invokes `make_room_admin` for the admin, retries the CS kick

#### Scenario: Force-kick endpoint not invoked on Synapse

- **WHEN** any RemoveMember fallback path runs on SynapseMatrixOps
- **THEN** no HTTP request is sent to `POST /_synapse/admin/v1/rooms/<roomID>/kick` (that endpoint does not exist — see contracts doc §3); the classified force-join / `make_room_admin` sender recovery is used instead

### Requirement: ReconcileMembers SHALL drive room membership to a desired set

`ReconcileMembers(ctx, roomID, desired []MemberSpec)` SHALL converge the room's membership to the desired set: members in `desired` but not currently joined are added (via AddMember); members currently joined but not in `desired` are removed (via RemoveMember). The implementation SHALL never remove the homeserver admin user implicitly (matching existing behavior at `provisioner.go:1160`). Per-user errors SHALL be collected; the first error is returned after processing all users (best-effort semantics).

#### Scenario: Reconcile adds missing members

- **WHEN** `ReconcileMembers` is called with `desired=[@a,@b]` and the room currently contains only `[@a]`
- **THEN** `AddMember(roomID, @b)` is invoked and the call returns nil on success

#### Scenario: Reconcile removes extra members via RemoveMember fallback

- **WHEN** `ReconcileMembers` finds a joined user not in `desired`
- **THEN** `RemoveMember` is invoked for that user; if CS kick fails on Synapse, the classified force-join / make_room_admin sender recovery runs inside RemoveMember — ReconcileMembers itself does not reimplement fallback

### Requirement: AppService operations SHALL adapt to declarative-vs-runtime model transparently

`RegisterAppService` SHALL succeed without issuing any registration HTTP when running against Synapse (declarative model) — it SHALL only invoke `SmokeTestAppService`. On failure, it SHALL return an error directing the operator to Helm values `matrix.appservice.*`. On Tuwunel, `RegisterAppService` SHALL issue `!admin appservices register` (existing behavior). `UnregisterAppService` SHALL issue `!admin appservices unregister` on Tuwunel and SHALL return an error directing to Helm on Synapse (no HTTP issued).

#### Scenario: RegisterAppService on Synapse = smoke-test only

- **WHEN** `RegisterAppService(ctx, reg)` runs on SynapseMatrixOps
- **THEN** the implementation invokes `SmokeTestAppService` and returns nil on success without issuing any `/_synapse/admin/*` request

#### Scenario: RegisterAppService on Tuwunel = admin bot register

- **WHEN** `RegisterAppService(ctx, reg)` runs on TuwunelMatrixOps
- **THEN** the implementation issues `"!admin appservices register\n```yaml\n...\n```"` to the admin bot (existing behavior preserved)

#### Scenario: UnregisterAppService on Synapse always errors

- **WHEN** `UnregisterAppService(ctx, "agentteams-controller")` runs on SynapseMatrixOps
- **THEN** the call returns a non-nil error mentioning Helm and `helm upgrade`, without issuing any HTTP

### Requirement: User-provisioning methods SHALL map to provider-appropriate admin APIs

`ProvisionUser` SHALL create or update a user with a password. On Synapse it SHALL use `PUT /_synapse/admin/v2/users/{id}` (current `SynapseClient.EnsureUser` behavior). On Tuwunel it SHALL use the existing `/register` + `registration_token` flow. `ResetUserPassword` SHALL map to `POST /_synapse/admin/v1/reset_password/{id}` on Synapse and `!admin users reset-password` on Tuwunel. `DeactivateUser` SHALL map to `POST /_synapse/admin/v1/deactivate/{id}` on Synapse and `!admin users deactivate` on Tuwunel.

#### Scenario: ProvisionUser on Synapse uses admin v2 PUT

- **WHEN** `ProvisionUser(ctx, UserSpec{Username:"alice", Password:"pw"})` runs on SynapseMatrixOps
- **THEN** the implementation issues `PUT /_synapse/admin/v2/users/@alice:dom` with `{"password":"pw","displayname":"alice"}`

#### Scenario: DeactivateUser on Synapse uses admin deactivate endpoint

- **WHEN** `DeactivateUser(ctx, userID)` runs on SynapseMatrixOps
- **THEN** the implementation issues `POST /_synapse/admin/v1/deactivate/<userID>` (not an `!admin` chat command)

### Requirement: Metadata and messaging methods SHALL recover from sender-not-in-room on Synapse

`SetRoomMetadata`, `RenameRoom`, and `SendSystemMessage` SHALL use the same classified sender recovery as `AddMember` on Synapse — force-join via `POST /_synapse/admin/v1/join/{roomID}` when the CS API returns sender-not-in-room, `make_room_admin` when it returns an insufficient-power error — then retry the CS operation. These methods take an optional business-level "actor" hint (e.g., "team-admin"); when no hint is given, the implementation uses the admin identity and the recovery path applies.

#### Scenario: SetRoomMetadata recovers via force-join when sender not in room

- **WHEN** `SetRoomMetadata(ctx, roomID, meta)` runs on SynapseMatrixOps and the CS `PUT /rooms/<roomID>/state/room.meta` returns `403 M_FORBIDDEN "User @admin:dom not in room"`
- **THEN** the implementation force-joins the sender via `POST /_synapse/admin/v1/join/{roomID}`, retries the state PUT, and returns nil on success

### Requirement: Query methods SHALL work cross-room on both providers

`ListRoomMembers`, `ListJoinedRooms`, and `IsUserInRoom` SHALL work without requiring the operator to be joined, on both providers. On Synapse they benefit from admin's `check_user_in_room` bypass (`synapse/api/auth/base.py:206`). On Tuwunel they use the admin bot's cross-room privilege.

#### Scenario: ListRoomMembers works for any room

- **WHEN** `ListRoomMembers(ctx, roomID)` is called for a room the admin is not joined to
- **THEN** the call succeeds on both Tuwunel and Synapse (admin bypass)

### Requirement: HealthCheck SHALL probe homeserver reachability without side effects

`HealthCheck(ctx)` SHALL return nil iff the homeserver is reachable and accepting requests. It SHALL NOT create or modify any user, room, or state. On both providers it SHALL issue a login attempt with invalid credentials and treat any non-connection error (401/403) as success (reachable).

#### Scenario: HealthCheck returns nil when homeserver up

- **WHEN** `HealthCheck` is called and the homeserver responds with `401 M_FORBIDDEN` to a bogus login
- **THEN** the call returns nil

#### Scenario: HealthCheck retries on connection error

- **WHEN** the homeserver is not yet listening and `HealthCheck` is invoked during controller startup
- **THEN** the call retries until the homeserver responds or a timeout elapses (existing Initializer behavior preserved)

### Requirement: TuwunelMatrixOps SHALL preserve existing behavior byte-for-byte

The TuwunelMatrixOps implementation SHALL reproduce every observable behavior of the current Tuwunel path: same error strings returned to the business layer, same idempotency rules, same admin-bot command formats, same smoke-test retry counts and intervals, same retry semantics in `RegisterAppService`. The Phases 1–5 migration SHALL NOT introduce any behavior change observable from the business layer when `AGENTTEAMS_MATRIX_PROVIDER=tuwunel` (or unset).

#### Scenario: Existing Tuwunel unit tests pass unmodified

- **WHEN** the existing test suite (`provisioner_team_test.go`, `client_test.go`, `appservice_test.go`, etc.) is run after migration
- **THEN** all previously-passing tests continue to pass without modification to their assertions about error messages, admin-bot commands, or call counts
