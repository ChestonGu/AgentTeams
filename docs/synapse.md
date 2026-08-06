# Synapse Matrix Support

AgentTeams ships with two Matrix homeserver options: **[Synapse](https://github.com/element-hq/synapse) 1.127+** (the default for Helm installs as of this work) and **Tuwunel** (a conduwuit fork, used by the embedded all-in-one installer and by older releases). Both providers are wired behind the same business-level `MatrixOps` abstraction, so the controller's orchestration logic, the Manager Agent, and every Worker behave identically regardless of which homeserver stores the rooms and accounts.

This guide covers the four things operators actually need to know:

1. How to switch the provider (Tuwunel → Synapse).
2. How the **declarative** AppService registration model differs from Tuwunel's runtime registration.
3. The namespace security model that keeps an `as_token` from impersonating non-AgentTeams users on a shared homeserver.
4. Token rotation, including why Synapse cannot rotate at runtime.

For the internal design rationale (why the abstraction looks the way it does, the exact error strings Synapse 1.127 emits, and the per-method fallback strategy) see [`design/synapse-support.md`](../design/synapse-support.md) and [`design/synapse-interface-contracts.md`](../design/synapse-interface-contracts.md).

## When to use which provider

| Provider | When to pick it |
|----------|-----------------|
| `synapse` (default for Helm) | Production / Kubernetes installs. The Helm chart deploys Synapse + Postgres as StatefulSets and renders the AppService registration declaratively. Pick this when you need Synapse-grade federation, moderation, audit tooling, or when your compliance team mandates Synapse. |
| `tuwunel` | The embedded all-in-one install (single machine / Docker Compose). Lighter weight than Synapse; the installer brings it up and the controller registers the AppService at runtime. Pick this for local dev or single-node deployments where the full Synapse+Postgres stack is overkill. |

Both providers implement the full `MatrixOps` surface (33 methods across room lifecycle, room membership, room metadata and messaging, queries, user identity, and AppService governance). The cross-implementation equivalence suite in `agentteams-controller/internal/matrix/ops_exhaustive_test.go` pins behavior parity for every method.

## Switching the provider

The provider is selected by the `AGENTTEAMS_MATRIX_PROVIDER` environment variable, which the Helm chart sets from `matrix.provider` (default `synapse`). Valid values are `synapse` (default when the variable is empty or unset inside the container) and `tuwunel`. The provider name is compared case-insensitively after trimming whitespace; any other value fails fast at controller startup.

The default Helm install brings up Synapse + Postgres. `matrix.provider=synapse` is the chart default, so a plain install already lands on Synapse:

```bash
helm install agentteams higress.io/agentteams \
  -n agentteams-system --create-namespace \
  --set credentials.llmApiKey=<key> \
  --set credentials.adminPassword=<admin-password> \
  --set gateway.publicURL=http://localhost:18080
```

> **Override the AppService tokens in production.** The chart ships `change-me-as-token` / `change-me-hs-token` defaults so a first install works out of the box (Helm cannot sync auto-generated tokens across the two Secrets the declarative model needs). Override both before any real deployment:
>
> ```bash
> helm install agentteams higress.io/agentteams \
>   -n agentteams-system --create-namespace \
>   --set credentials.llmApiKey=<key> \
>   --set credentials.adminPassword=<admin-password> \
>   --set gateway.publicURL=http://localhost:18080 \
>   --set matrix.appservice.asToken=$(openssl rand -hex 32) \
>   --set matrix.appservice.hsToken=$(openssl rand -hex 32)
> ```

Switching an existing release to Tuwunel (or back) is a `helm upgrade` — the controller restarts and rebinds to the new homeserver:

```bash
helm upgrade agentteams higress.io/agentteams \
  -n agentteams-system --reuse-values \
  --set matrix.provider=tuwunel
```

> **Heads-up on switching.** Tuwunel and Synapse are separate databases. Rooms and accounts created under one homeserver do not migrate automatically; switching the provider points the controller at a fresh homeserver. Plan a maintenance window and re-provision Workers/Teams after the switch.

## Declarative AppService registration

AgentTeams registers itself as a Matrix **Application Service** so it can provision Worker and Human Matrix accounts without passwords. The two providers handle this registration very differently:

| Aspect | Tuwunel (runtime) | Synapse (declarative) |
|--------|-------------------|-----------------------|
| Registration lifecycle | Controller sends `!admin appservices register` with a YAML body to the admin bot at startup. Token rotation re-sends the command. | Registration lives in a YAML file mounted from a Helm-rendered Secret; the homeserver loads it via `app_service_config_files:` in `homeserver.yaml`. No runtime API exists. |
| Token rotation | Supported via the AppService management endpoint (`RotateToken`). The controller unregisters the old registration and registers the new one in a single call. | **Not supported at runtime.** Rotate by updating `matrix.appservice.asToken` / `hsToken` in your Helm values and running `helm upgrade`; Synapse reloads the mounted file on restart. |
| Unregister | Controller sends `!admin appservices unregister <id>`. | Returns an error pointing the operator at Helm — there is no runtime unregister endpoint. Remove the AppService by deleting the `matrix.appservice.*` block and re-applying the chart. |
| Smoke test | After registration, the controller attempts an Application-Service login as the `sender_localpart` user; success confirms the `as_token` is live. | Same smoke test runs. If it fails, `RegisterAppService` reports an error that points the operator at the Helm-managed `app_service_config_files` rather than retrying runtime registration. |

### What the controller still does on Synapse

Even though registration is declarative, the controller still:

1. **Runs the smoke test at startup** to confirm the mounted registration is active. This is the only wire call Synapse makes for AppService governance.
2. **Reports a clear error when the smoke test fails**, naming the AppService ID and instructing the operator to update the chart's `matrix.appservice.*` values and the homeserver's `app_service_config_files` list, then re-apply.
3. **Refuses to unregister at runtime**, returning a static error that points at the Helm chart.

The runtime-rotation endpoint on the AppService management handler returns HTTP 501 on Synapse, because there is nothing the controller can do server-side to rotate a declaratively-managed token.

## Namespace security model

By default the AppService registration claims the **exclusive** `@.*:<domain>` user namespace. That regex means the `as_token` can impersonate **every** local user on the homeserver. This is only safe when the homeserver is exclusively AgentTeams-managed — i.e. AgentTeams provisions the homeserver and every local user on it. That is the only mode the embedded Tuwunel install and Helm's `matrix.mode=managed` setting permit.

If you point AgentTeams at a **shared or pre-existing** Synapse cluster that also hosts non-AgentTeams users (for example, your company's corporate Matrix deployment), the broad namespace would let the `as_token` impersonate those users. The escape hatch is `AGENTTEAMS_MATRIX_APPSERVICE_USER_NAMESPACE_REGEX`, which narrows the namespace so the `as_token` can only act on AgentTeams-managed localparts:

```bash
helm upgrade agentteams higress.io/agentteams \
  -n agentteams-system --reuse-values \
  --set matrix.provider=synapse \
  --set matrix.mode=existing \
  --set matrix.appservice.userNamespaceRegex='@agentteams-.*:your-server-name'
```

Then provision every AgentTeams-managed user (Workers, Humans, the Manager) under that prefix so the regex covers them. Helm's `00-validate.yaml` hook fails the install if `matrix.mode` is not `managed` and the namespace regex is empty, so this configuration mistake cannot slip through silently.

## Token rotation

### Tuwunel (runtime rotation)

```bash
# The AppService management endpoint accepts a rotation request; the controller
# unregisters the old registration and registers the new one in a single call.
# The new tokens are persisted to the runtime-env Secret and the controller
# restarts with them loaded.
helm upgrade agentteams higress.io/agentteams \
  -n agentteams-system --reuse-values \
  --set matrix.appservice.asToken=$(openssl rand -hex 32) \
  --set matrix.appservice.hsToken=$(openssl rand -hex 32)
```

The controller's `RegisterAppService` is idempotent across restarts: it runs the smoke test first, and if the existing registration's token already works, registration is a no-op. On a token mismatch it unregisters (best-effort) and re-registers.

### Synapse (restart-only rotation)

Synapse has no runtime AppService API, so the workflow is:

1. Generate new tokens.
2. Update `matrix.appservice.asToken` and `matrix.appservice.hsToken` in your Helm values.
3. `helm upgrade`. The Secret re-renders with the new tokens; Synapse reloads `app_service_config_files` on StatefulSet restart.
4. The controller's startup smoke test confirms the new `as_token` is live.

The AppService management handler's `RotateToken` endpoint returns **HTTP 501** on Synapse with a JSON body pointing the operator at this workflow — there is no server-side shortcut.

## How the controller recovers from "admin not in room"

A subtlety operators occasionally hit on Synapse: the controller's global admin user must be **joined** to a room (with sufficient power level) before it can invite, kick, rename, or write state events there. Synapse 1.127 enforces this strictly — unlike Tuwunel's admin bot, there is no super-admin bypass.

When the controller's admin leaves a room (for example, the team room deliberately excludes the global admin from its power levels), the controller recovers in-room operations via **`make_room_admin`** — the Synapse admin REST endpoint that force-joins a user **and** grants them power level 100:

```
POST /_synapse/admin/v1/rooms/{roomID}/make_room_admin
{"user_id": "<the operator who needs power>"}
```

This fallback fires automatically for:

- `AddMember` when the invite fails with `@admin not in room` or `You don't have permission to invite users`.
- `RemoveMember` when the kick fails with `You cannot kick user` or `@admin not in room`.
- `SetRoomMetadata` / `RenameRoom` / `SendSystemMessage` when the write fails with `User @x not in room` or `You don't have permission to post that to the room`.

The recovery is: call `make_room_admin` on the operator (the actor for metadata ops, the admin for system messages), then retry the original CS operation with the same token. On Tuwunel, the same scenarios escalate to the admin bot's `!admin users force-leave-room` command for kicks — there is no make_room_admin equivalent on the Tuwunel side because the admin bot already has cross-room privileges.

The equivalence suite pins both the Tuwunel escalation (admin-bot command) and the Synapse escalation (make_room_admin + retry) so a regression that swaps the two wires is caught immediately.

## Operational checklist for a Synapse deployment

1. **Provider and mode.** Set `matrix.provider=synapse` and `matrix.mode=existing` (if pointing at your own cluster) or `matrix.mode=managed` (if letting the chart deploy Synapse for you).
2. **Admin credentials.** The controller's `AGENTTEAMS_ADMIN_USER` must be a Synapse **server admin** — every `/_synapse/admin/v1/*` and `/_synapse/admin/v2/*` call returns 403 otherwise. The bootstrap job creates this user via `register_new_matrix_user` driven by `matrix.synapse.registrationSharedSecret`.
3. **AppService tokens.** Leave `matrix.appservice.asToken` / `hsToken` empty to auto-generate, or set them explicitly. The runtime-env Secret preserves generated values across upgrades.
4. **Namespace regex.** If the Synapse cluster hosts non-AgentTeams users, set `matrix.appservice.userNamespaceRegex` to a restrictive regex. The chart rejects an empty regex in `mode=existing`.
5. **Server admin for make_room_admin.** The admin user only needs server-admin rights; `make_room_admin` grants per-room power. No extra Synapse configuration is required for the in-room fallback to work.
6. **Smoke test at startup.** Watch the controller logs for `Matrix appservice token not active yet`. This is transient and self-heals once the declarative registration loads; the controller requeues quietly rather than logging a hard error.

## Related references

- [Helm values](../helm/agentteams/values.yaml) — `matrix.*` block with full inline documentation.
- [Architecture overview](architecture.md) — system-level view of the Manager / Worker / Matrix / Gateway / Storage split.
- [`design/synapse-support.md`](../design/synapse-support.md) — internal design rationale, including why the `MatrixOps` abstraction looks the way it does.
- [`design/synapse-interface-contracts.md`](../design/synapse-interface-contracts.md) — per-method Synapse 1.127 behavior contracts with exact error strings and source-line references.
- `agentteams-controller/internal/matrix/ops_exhaustive_test.go` — the cross-implementation equivalence suite that pins behavior parity.
