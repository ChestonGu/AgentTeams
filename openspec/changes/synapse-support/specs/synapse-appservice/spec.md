## Purpose

Enable the Application Service mode on Synapse 1.127 by loading the AS registration declaratively (Helm-rendered YAML mounted into the Synapse pod and referenced from `homeserver.yaml`), replacing Tuwunel's runtime admin-bot registration flow. Without this capability, passwordless user provisioning (Human SSO, AppService-mode workers) cannot work on Synapse because Synapse exposes no runtime AS registration API.

## ADDED Requirements

### Requirement: Helm chart SHALL render Application Service registration as a Secret when AppService mode is enabled for Synapse

When `matrix.provider=synapse` AND `matrix.mode=managed` AND `matrix.appservice.enabled=true`, the Helm chart SHALL render a Secret named `<synapse-fullname>-appservice` containing the AS registration YAML at key `agentteams-controller.yaml`. The YAML SHALL include `id`, `as_token`, `hs_token`, `sender_localpart`, `namespaces` (users NON-exclusive with regex covering the provisioned localparts, default `@.*:<server>`; aliases non-exclusive `#agentteams-.*:<server>`; rooms empty), `rate_limited: false`, and `url` (push URL or null). The users namespace MUST NOT be marked exclusive: Synapse 1.127 rejects all other user creation for an exclusively-claimed ID (including the shared-secret admin bootstrap and `PUT /_synapse/admin/v2/users/{id}`) with HTTP 400 `M_EXCLUSIVE`, which would make the admin user and every admin-provisioned user uncreatable.

#### Scenario: AS Secret rendered with required fields

- **WHEN** `helm template` is run with `matrix.provider=synapse`, `matrix.mode=managed`, `matrix.appservice.enabled=true`, `matrix.appservice.asToken=secret-as`, `matrix.appservice.hsToken=secret-hs`
- **THEN** a Secret named `<synapse-fullname>-appservice` is rendered with `stringData.agentteams-controller.yaml` containing `as_token: "secret-as"`, `hs_token: "secret-hs"`, `sender_localpart: "agentteams-controller"`, `id: "agentteams-controller"`, and user namespace regex matching `@agentteams-controller:<server>`

#### Scenario: AS Secret NOT rendered for Tuwunel

- **WHEN** `helm template` is run with `matrix.provider=tuwunel`
- **THEN** no Secret named `<synapse-fullname>-appservice` is rendered

#### Scenario: AS Secret NOT rendered when AppService disabled

- **WHEN** `helm template` is run with `matrix.provider=synapse`, `matrix.appservice.enabled=false`
- **THEN** no Secret named `<synapse-fullname>-appservice` is rendered

### Requirement: Synapse homeserver.yaml SHALL declare app_service_config_files when AppService mode is enabled

When `matrix.provider=synapse` AND `matrix.appservice.enabled=true`, the Synapse ConfigMap `homeserver.yaml` content SHALL include `app_service_config_files: [/as-registrations/agentteams-controller.yaml]`.

#### Scenario: ConfigMap includes app_service_config_files

- **WHEN** `helm template` is run with `matrix.provider=synapse`, `matrix.appservice.enabled=true`
- **THEN** the Synapse ConfigMap `homeserver.yaml` data key contains the line `app_service_config_files:` followed by `- /as-registrations/agentteams-controller.yaml`

#### Scenario: ConfigMap excludes app_service_config_files when AS disabled

- **WHEN** `helm template` is run with `matrix.provider=synapse`, `matrix.appservice.enabled=false`
- **THEN** the Synapse ConfigMap `homeserver.yaml` data key does NOT contain `app_service_config_files`

### Requirement: Synapse StatefulSet SHALL mount the AS Secret when AppService mode is enabled

When `matrix.provider=synapse` AND `matrix.appservice.enabled=true`, the Synapse StatefulSet SHALL mount the AS Secret at `/as-registrations/` (readOnly), with the YAML file exposed at `/as-registrations/agentteams-controller.yaml`.

#### Scenario: Volume and volumeMount added

- **WHEN** `helm template` is run with `matrix.provider=synapse`, `matrix.appservice.enabled=true`
- **THEN** the Synapse StatefulSet pod template contains a `volumes:` entry with `secret.secretName: <synapse-fullname>-appservice` and a corresponding `volumeMounts:` entry with `mountPath: /as-registrations` and `readOnly: true`

### Requirement: Helm SHALL validate required AS fields and namespace safety

`templates/00-validate.yaml` SHALL fail Helm install/upgrade when `matrix.provider=synapse` AND `matrix.appservice.enabled=true` AND any of: (a) `matrix.appservice.asToken` is empty; (b) `matrix.appservice.hsToken` is empty; (c) `matrix.appservice.userNamespaceRegex` is empty AND `matrix.mode != managed` (the default `@.*:<server>` namespace is unsafe against shared homeservers).

#### Scenario: Missing asToken fails validation

- **WHEN** `helm install` is run with `matrix.provider=synapse`, `matrix.appservice.enabled=true`, `matrix.appservice.asToken` unset
- **THEN** Helm fails with a `required` template error mentioning `matrix.appservice.asToken`

#### Scenario: Default namespace rejected for unmanaged Synapse

- **WHEN** `helm install` is run with `matrix.provider=synapse`, `matrix.mode=external`, `matrix.appservice.enabled=true`, `matrix.appservice.userNamespaceRegex` unset
- **THEN** Helm fails with an error explaining the default `@.*:<server>` namespace is unsafe for shared homeservers

### Requirement: AS env variables SHALL be injected into controller runtime Secret on Synapse

When `matrix.provider=synapse` AND `matrix.appservice.enabled=true`, the controller runtime-env Secret SHALL include `AGENTTEAMS_MATRIX_APPSERVICE_ENABLED=true`, `AGENTTEAMS_MATRIX_APPSERVICE_ID`, `AGENTTEAMS_MATRIX_APPSERVICE_AS_TOKEN`, `AGENTTEAMS_MATRIX_APPSERVICE_HS_TOKEN`, `AGENTTEAMS_MATRIX_APPSERVICE_SENDER_LOCALPART`, and (when set) `AGENTTEAMS_MATRIX_APPSERVICE_USER_NAMESPACE_REGEX`.

#### Scenario: AS env injected into runtime Secret

- **WHEN** `helm template` is run with `matrix.provider=synapse`, `matrix.appservice.enabled=true`, `matrix.appservice.asToken=tok`
- **THEN** the runtime-env Secret data contains `AGENTTEAMS_MATRIX_APPSERVICE_AS_TOKEN` matching `tok`

### Requirement: AS token rotation endpoint SHALL return 501 on Synapse

`POST /api/v1/appservice/rotate-token` SHALL return HTTP 501 with a body explaining that rotation must be performed via `helm upgrade` (update `matrix.appservice.asToken` / `matrix.appservice.hsToken`) followed by restarting both the controller Pod and the Synapse Pod, when the Matrix provider is Synapse. On Tuwunel the endpoint SHALL retain its existing behavior (this existing behavior is itself migrated to go through MatrixOps in Phase 4 of matrix-ops, but the 501-on-Synapse contract is fixed from the start).

#### Scenario: RotateToken returns 501 on Synapse

- **WHEN** a POST request with JSON body `{"as_token":"new"}` is sent to `/api/v1/appservice/rotate-token` and the controller was started with `AGENTTEAMS_MATRIX_PROVIDER=synapse`
- **THEN** the response is HTTP 501 and the body mentions `helm upgrade` and pod restart

#### Scenario: RotateToken unchanged on Tuwunel

- **WHEN** the same POST request is sent and the controller was started with `AGENTTEAMS_MATRIX_PROVIDER=tuwunel` (or unset)
- **THEN** the response follows the existing behavior (200 on success, 500 on failure) — no regression
