## Purpose

Let a single controller binary target either Tuwunel or Synapse as its Matrix homeserver by selecting the `MatrixOps` implementation at startup from an environment variable. This keeps the existing Tuwunel code path byte-for-byte unchanged while enabling the Synapse-specific behavior described in the sibling `matrix-ops` and `synapse-appservice` specs.

## ADDED Requirements

### Requirement: Controller SHALL select MatrixOps implementation by AGENTTEAMS_MATRIX_PROVIDER

The controller SHALL read `AGENTTEAMS_MATRIX_PROVIDER` at startup. When the value is `synapse` (case-insensitive), the controller SHALL construct a `SynapseMatrixOps` (backed by a Synapse admin client + CS API client). When the value is `tuwunel` or unset, the controller SHALL construct a `TuwunelMatrixOps` (the default and only previously-supported behavior). Any other value SHALL fail startup with a clear error listing valid values.

#### Scenario: Synapse provider selected

- **WHEN** controller starts with `AGENTTEAMS_MATRIX_PROVIDER=synapse`
- **THEN** the constructed MatrixOps is a `*SynapseMatrixOps`, and the `matrix.Config.Provider` field is `"synapse"`

#### Scenario: Tuwunel provider selected by default

- **WHEN** controller starts with `AGENTTEAMS_MATRIX_PROVIDER` unset
- **THEN** the constructed MatrixOps is a `*TuwunelMatrixOps`, and `matrix.Config.Provider` is `"tuwunel"`

#### Scenario: Unknown provider fails startup

- **WHEN** controller starts with `AGENTTEAMS_MATRIX_PROVIDER=dendrite`
- **THEN** startup fails with an error listing valid values (`tuwunel`, `synapse`)

### Requirement: The selected MatrixOps SHALL be injected into every business consumer

The single MatrixOps instance constructed at startup SHALL be injected into `Provisioner`, `Initializer`, and the HTTP handler constructing path (for `AppServiceHandler`/rotation). No business consumer SHALL construct its own Matrix client or MatrixOps instance. This replaces the existing bug where `appservice_mgmt_handler.go` hardcodes `matrix.NewTuwunelClient`.

#### Scenario: Provisioner receives injected MatrixOps

- **WHEN** `NewProvisioner` is called during startup
- **THEN** it receives the MatrixOps instance via `ProvisionerConfig.MatrixOps` (the `Matrix matrix.Client` field is removed or deprecated)

#### Scenario: appservice_mgmt_handler no longer constructs clients

- **WHEN** `RotateToken` needs to perform an AS rotation
- **THEN** it calls a method on the injected MatrixOps (or a dedicated rotation helper backed by MatrixOps); it does NOT call `matrix.NewTuwunelClient` or any `New*Client`

### Requirement: matrix.Config SHALL expose the active Provider to downstream consumers

`matrix.Config` SHALL carry a `Provider` string field set from the same env variable. HTTP handlers and other consumers that need provider-specific dispatch (e.g., the AS token-rotation endpoint returning 501 on Synapse) SHALL read this field rather than performing type assertions on the MatrixOps implementation.

#### Scenario: RotateToken handler branches on Provider

- **WHEN** the RotateToken handler receives a request and `matrixCfg.Provider == "synapse"`
- **THEN** the handler returns 501 without delegating to MatrixOps

### Requirement: Helm chart SHALL pass the provider to the controller runtime-env Secret

The controller runtime-env Secret SHALL include `AGENTTEAMS_MATRIX_PROVIDER` set from `.Values.matrix.provider` (default `tuwunel`).

#### Scenario: Provider env injected for Synapse

- **WHEN** `helm template` is run with `matrix.provider=synapse`
- **THEN** the runtime-env Secret data contains `AGENTTEAMS_MATRIX_PROVIDER=synapse`

#### Scenario: Provider defaults to tuwunel

- **WHEN** `helm template` is run with `matrix.provider` unset
- **THEN** the runtime-env Secret data contains `AGENTTEAMS_MATRIX_PROVIDER=tuwunel` (chart SHOULD be explicit even though the controller treats unset as tuwunel)
