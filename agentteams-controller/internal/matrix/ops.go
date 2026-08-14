package matrix

import (
	"context"
	"fmt"
)

// MatrixOps is the business-level abstraction over Matrix homeserver
// operations. The controller's business layer (Provisioner, Initializer, HTTP
// handlers) depends on this interface and is fully decoupled from Matrix
// protocol details (token selection, API paths, admin-bot commands,
// provider-specific fallback strategies).
//
// Methods are grouped by business capability, not by Matrix protocol verb.
// Each method's signature accepts business-typed specs/values (RoomSpec,
// MemberSpec, RoomMetadata) and returns business refs (RoomRef, UserRef).
// Internal protocol details (which token to use, whether to retry, which
// admin API to call) are decided inside the implementation.
//
// There are two implementations:
//   - TuwunelMatrixOps: admin ops via the Tuwunel admin bot ("!admin ...")
//   - SynapseMatrixOps: admin ops via Synapse REST APIs, with make_room_admin
//     fallback for in-room CS operations
//
// Phase 1 (this file) declares the 4 core room-ops methods. Subsequent phases
// append methods grouped under the 6 business capabilities:
//   - User Identity & Credentials (~10 methods)        [Phase 4]
//   - Room Lifecycle (~5 methods)                      [Phase 1 + Phase 2]
//   - Room Membership (~8 methods)                     [Phase 1 + Phase 2]
//   - Room Metadata & Messaging (~3 methods)           [Phase 3]
//   - AppService Governance (~3 methods)               [Phase 4]
//   - Queries & Ops (~3 methods)                       [Phase 3]
type MatrixOps interface {
	// === Phase 1: Room Lifecycle (core) + Room Membership (core) ===

	// CreateRoom creates a new Matrix room described by spec. The room alias
	// in spec.AliasLocalpart makes the call idempotent: if a room with that
	// alias already exists, the implementation resolves it and returns
	// RoomRef{RoomID: <existing>, Created: false} rather than failing.
	CreateRoom(ctx context.Context, spec RoomSpec) (*RoomRef, error)

	// DissolveRoom removes the room from the homeserver on a best-effort,
	// fire-and-forget basis. The caller does not need to specify which user
	// dissolves the room or be in the room. On Tuwunel: admin bot
	// "!admin rooms delete-room". On Synapse: DELETE /_synapse/admin/v2/rooms/{id}.
	DissolveRoom(ctx context.Context, roomID string) error

	// AddMember invites userID into roomID. Idempotent: returns nil if the
	// user is already joined/invited. On Synapse, when the operator is not in
	// the room or lacks PL, the implementation recovers via make_room_admin.
	AddMember(ctx context.Context, roomID, userID string) error

	// InviteMember invites userID to roomID. member.ActorToken when set
	// authenticates the invite as that user (e.g. a team leader already
	// joined to the room); empty falls back to the admin identity (AddMember
	// semantics with provider-specific escalation).
	InviteMember(ctx context.Context, roomID, userID string, member MemberSpec) error

	// RemoveMember removes userID from roomID. Idempotent: returns nil if the
	// user is not currently in the room. On Tuwunel, falls back to admin bot
	// "!admin users force-leave-room" when CS kick fails. On Synapse, falls
	// back to make_room_admin + CS kick retry.
	RemoveMember(ctx context.Context, roomID, userID, reason string) error

	// === Phase 2: Room Lifecycle (alias) + Room Membership (full) ===

	// ReconcileMembers converges roomID's membership to the desired set.
	// Members in `desired` but not currently joined are added (via
	// AddMember); members currently joined but not in `desired` are removed
	// (via RemoveMember). The implementation NEVER removes the homeserver
	// admin user implicitly (matching pre-abstraction behavior). Per-user
	// errors are collected and the first error is returned after processing
	// every user (best-effort semantics). spec.ActorToken, when set, lets a
	// caller act as a specific user (e.g. a team admin before the global
	// controller admin is joined); the token is used to list members and to
	// issue the CS invite/kick.
	ReconcileMembers(ctx context.Context, roomID string, desired []MemberSpec) error

	// JoinRoom joins roomID as the user described by member. When
	// member.ActorToken is empty, the implementation uses the admin identity.
	// Idempotent when already joined.
	JoinRoom(ctx context.Context, roomID string, member MemberSpec) error

	// LeaveRoom leaves roomID as the user described by member. When
	// member.ActorToken is empty, the implementation uses the admin identity.
	// Idempotent (user already left → nil).
	LeaveRoom(ctx context.Context, roomID string, member MemberSpec) error

	// ForceLeaveAllRooms makes the user described by member leave every room
	// they are currently joined to. Best-effort: errors leaving individual
	// rooms are logged but not returned. When member.ActorToken is empty, the
	// implementation uses the admin identity.
	ForceLeaveAllRooms(ctx context.Context, member MemberSpec) error

	// ReleaseRoomAlias removes the given full alias ("#localpart:domain") from
	// the homeserver. The underlying room is left intact (only the alias is
	// detached). Idempotent (alias not present → nil).
	ReleaseRoomAlias(ctx context.Context, alias string) error

	// ResolveRoomAlias resolves a full alias ("#localpart:domain") to its room
	// ID. ok is false when the alias does not exist.
	ResolveRoomAlias(ctx context.Context, alias string) (roomID string, ok bool, err error)

	// ArchiveRoom renames roomID to name, marking a preserved Team room with a
	// stable deleted suffix so humans can distinguish it from active rooms
	// after its alias is released. Uses member.ActorToken when set (e.g. the
	// team admin token) else the admin identity.
	ArchiveRoom(ctx context.Context, roomID, name string, member MemberSpec) error

	// === Phase 3: Room Metadata & Messaging + Queries & Ops ===

	// SetRoomMetadata writes the room.meta custom state event for roomID —
	// the business metadata (schemaVersion / roomKind / lifecycle / createdBy
	// plus room-specific fields such as teamName, workerName, members).
	// member.ActorToken, when set, authenticates the write as that user
	// (e.g. the team admin owning a team room); empty falls back to the
	// admin identity.
	SetRoomMetadata(ctx context.Context, roomID string, content map[string]interface{}, member MemberSpec) error

	// RenameRoom sets the human-readable name (m.room.name) of roomID.
	// Uses member.ActorToken when set (e.g. the team admin), else the admin
	// identity.
	RenameRoom(ctx context.Context, roomID, name string, member MemberSpec) error

	// SendSystemMessage sends a plain-text system message to roomID as the
	// homeserver admin identity. Used by the controller to inject
	// system-level prompts (e.g. the first-boot Manager onboarding welcome).
	SendSystemMessage(ctx context.Context, roomID, body string) error

	// ListRoomMembers returns the users currently joined/invited to roomID.
	// member.ActorToken, when set, authenticates the read as that user;
	// empty falls back to the admin identity (which bypasses in-room
	// checks).
	ListRoomMembers(ctx context.Context, roomID string, member MemberSpec) ([]RoomMember, error)

	// ListJoinedRooms returns the room IDs the user described by member is
	// currently joined to. Empty ActorToken falls back to the admin identity.
	ListJoinedRooms(ctx context.Context, member MemberSpec) ([]string, error)

	// IsUserInRoom reports whether userID is currently joined (membership
	// "join") to roomID. Pure read.
	IsUserInRoom(ctx context.Context, roomID, userID string) (bool, error)

	// IsManagerJoinedDM reports whether the Manager's Matrix user
	// (localpart "manager") is currently `join`ed to the supplied DM room.
	// Pure read; safe to poll on every reconcile while waiting for the
	// agent's first /sync to land its auto-join.
	IsManagerJoinedDM(ctx context.Context, roomID string) (bool, error)

	// HealthCheck verifies the homeserver is reachable. Returns nil when the
	// server responds at the HTTP level (even with an auth rejection — a
	// 401/403 means it is up); returns a transport error (connection
	// refused, DNS failure, timeout, EOF) when it is not reachable.
	HealthCheck(ctx context.Context) error

	// === Phase 4: User Identity & Credentials + AppService Governance ===

	// ProvisionUser provisions (or logs in) a Matrix user in password mode.
	// spec.Password empty → the implementation generates a secure random
	// password. Returns the business user reference AND the credentials —
	// UserRef deliberately omits the sensitive AccessToken/Password, which
	// are carried by the returned UserCredentials.
	ProvisionUser(ctx context.Context, spec UserSpec) (*UserRef, *UserCredentials, error)

	// ProvisionUserViaAppService provisions (or logs in) a Matrix user via
	// the Application Service flow (as_token authentication, no password).
	// Requires the AppService to be registered with the homeserver. Returns
	// the business user reference plus the credentials (Password is empty).
	ProvisionUserViaAppService(ctx context.Context, localpart string) (*UserRef, *UserCredentials, error)

	// LoginUser obtains a fresh access token for an existing user via
	// username/password login.
	LoginUser(ctx context.Context, username, password string) (string, error)

	// LoginUserViaAppService obtains a fresh access token for an existing
	// user via the Application Service login flow
	// (m.login.application_service), authenticated with the as_token.
	LoginUserViaAppService(ctx context.Context, localpart string) (string, error)

	// ResetUserPassword sets a user's password using the admin identity.
	// On Tuwunel: admin bot "!admin users reset-password". On Synapse:
	// POST /_synapse/admin/v1/reset_password/{userId}.
	ResetUserPassword(ctx context.Context, userID, password string) error

	// DeactivateUser deactivates a user account so it can no longer log in.
	// On Tuwunel: admin bot "!admin users deactivate". On Synapse:
	// POST /_synapse/admin/v1/deactivate/{userId}.
	DeactivateUser(ctx context.Context, userID string) error

	// SetUserDisplayName updates a user's profile displayname using the
	// supplied access token. When accessToken is empty the implementation
	// falls back to the admin identity.
	SetUserDisplayName(ctx context.Context, userID, accessToken, displayName string) error

	// VerifyUserAccessToken checks whether a user access token is still
	// valid (GET /_matrix/client/v3/account/whoami). Returns nil if valid.
	VerifyUserAccessToken(ctx context.Context, accessToken string) error

	// UserIDFor builds the full Matrix user ID "@<localpart>:<domain>" from
	// a localpart. Pure formatting (no I/O, no error).
	UserIDFor(localpart string) string

	// BackfillLegacyPassword sets a user's password during the legacy
	// password backfill migration (AppService → password mode transition).
	// Same underlying admin operation as ResetUserPassword, with a distinct
	// business intent (bulk migration).
	BackfillLegacyPassword(ctx context.Context, userID, password string) error

	// RegisterAppService registers the controller's Application Service with
	// the homeserver. On Tuwunel: admin bot registration with smoke-test
	// idempotency and unregister-before-register fallback. On Synapse:
	// registrations are declarative (Helm-managed) — the implementation
	// runs the smoke test and reports an error if the AppService is not
	// active.
	RegisterAppService(ctx context.Context, reg AppServiceRegistration) error

	// UnregisterAppService removes an Application Service registration by
	// ID. On Tuwunel: admin bot command. On Synapse: returns an error
	// pointing the operator to Helm (declarative registrations cannot be
	// removed at runtime).
	UnregisterAppService(ctx context.Context, id string) error

	// SmokeTestAppService verifies the AppService is active by attempting an
	// AS login as the sender_localpart user.
	SmokeTestAppService(ctx context.Context) error
}

// Compile-time assertions that both implementations satisfy MatrixOps.
// These will fail the build if either implementation drifts from the
// interface; they're written here (rather than next to each impl) to keep
// the interface file as the single source of truth that gates both impls.
var _ MatrixOps = (*TuwunelMatrixOps)(nil)
var _ MatrixOps = (*SynapseMatrixOps)(nil)

// ---------------------------------------------------------------------------
// Legacy conversion layer
// ---------------------------------------------------------------------------

// legacyClient is the protocol-level surface the LegacyClientOps bridge needs:
// the provider-agnostic Client interface PLUS the Tuwunel-only admin methods
// (AdminCommand / SetPasswordAsAdmin / RegisterAppService / UnregisterAppService)
// and the user-provisioning methods (EnsureUser / SendMessageAsAdmin) that were
// removed from Client because their default implementations are tightly coupled
// to Tuwunel specifics (registration_token flow, admin-bot orphan recovery,
// cached admin token). Both *TuwunelClient and *SynapseClient implement these
// on the concrete type; the test fake (fakeTeamMatrix) satisfies the composite
// interface by implementing every method.
type legacyClient interface {
	Client
	AdminCommand(ctx context.Context, command string) error
	SetPasswordAsAdmin(ctx context.Context, userID, password string) error
	RegisterAppService(ctx context.Context, reg AppServiceRegistration) error
	UnregisterAppService(ctx context.Context, id string) error
	EnsureUser(ctx context.Context, req EnsureUserRequest) (*UserCredentials, error)
	SendMessageAsAdmin(ctx context.Context, roomID, body string) error
}

// LegacyClientOps adapts a protocol-level matrix client into the MatrixOps
// surface with Tuwunel semantics. It exists as the conversion layer for
// tests and any transitional caller that still holds a Tuwunel-style client
// instead of a concrete MatrixOps implementation. Production code constructs a
// concrete *TuwunelMatrixOps or *SynapseMatrixOps via NewOps; this bridge is
// test-only and preserves the pre-abstraction service-layer call shapes so
// existing fakes keep working unchanged.
type LegacyClientOps struct {
	client legacyClient
	config Config
}

// NewLegacyClientOps wraps client into a MatrixOps with Tuwunel semantics.
// client must satisfy the composite legacyClient interface (Client plus the
// Tuwunel-only admin methods); *TuwunelClient and the test fakes both do.
func NewLegacyClientOps(client legacyClient, config Config) *LegacyClientOps {
	return &LegacyClientOps{client: client, config: config}
}

// CreateRoom translates the business RoomSpec into a protocol
// CreateRoomRequest (same translation as TuwunelMatrixOps) and delegates to
// the wrapped client.
func (o *LegacyClientOps) CreateRoom(ctx context.Context, spec RoomSpec) (*RoomRef, error) {
	info, err := o.client.CreateRoom(ctx, roomSpecToRequest(spec))
	if err != nil {
		return nil, err
	}
	return &RoomRef{RoomID: info.RoomID, Created: info.Created}, nil
}

// DissolveRoom sends the fire-and-forget `!admin rooms delete-room <roomID>`
// command to the Tuwunel admin bot (same command the service layer's
// deleteRoom sent).
func (o *LegacyClientOps) DissolveRoom(ctx context.Context, roomID string) error {
	if roomID == "" {
		return nil
	}
	cmd := fmt.Sprintf("!admin rooms delete-room %s", roomID)
	return o.client.AdminCommand(ctx, cmd)
}

// AddMember invites via the admin token (idempotent when already joined).
func (o *LegacyClientOps) AddMember(ctx context.Context, roomID, userID string) error {
	return o.client.InviteToRoom(ctx, roomID, userID)
}

// InviteMember implements MatrixOps.InviteMember for the legacy bridge. When
// member.ActorToken is set the invite is issued as that user
// (InviteToRoomWithToken); otherwise it falls back to the admin identity via
// InviteToRoom.
func (o *LegacyClientOps) InviteMember(ctx context.Context, roomID, userID string, member MemberSpec) error {
	if member.ActorToken != "" {
		return o.client.InviteToRoomWithToken(ctx, roomID, userID, member.ActorToken)
	}
	return o.client.InviteToRoom(ctx, roomID, userID)
}

// RemoveMember kicks via the admin token; escalates to the admin bot
// force-leave-room command when the kick fails due to insufficient power.
func (o *LegacyClientOps) RemoveMember(ctx context.Context, roomID, userID, reason string) error {
	err := o.client.KickFromRoom(ctx, roomID, userID, reason)
	if err == nil {
		return nil
	}
	if !shouldForceLeaveAfterKickError(err) {
		return err
	}
	cmd := fmt.Sprintf("!admin users force-leave-room %s %s", userID, roomID)
	if cmdErr := o.client.AdminCommand(ctx, cmd); cmdErr != nil {
		return fmt.Errorf("kick %s from %s failed (%v) and force-leave-room command failed: %w",
			userID, roomID, err, cmdErr)
	}
	return nil
}

// === Phase 2: Room Lifecycle (alias) + Room Membership (full) ===

// adminToken resolves a fresh admin access token via Login. The legacy bridge
// holds only a protocol-level Client (no token cache), matching the
// pre-abstraction service layer which called client.Login directly.
func (o *LegacyClientOps) adminToken(ctx context.Context) (string, error) {
	return o.client.Login(ctx, o.config.AdminUser, o.config.AdminPassword)
}

// ReconcileMembers delegates to the shared convergence core, using the
// wrapped client (Tuwunel semantics) and the legacy AddMember/RemoveMember
// escalation for the non-actor path.
func (o *LegacyClientOps) ReconcileMembers(ctx context.Context, roomID string, desired []MemberSpec) error {
	return reconcileMembersImpl(ctx, o, o.client, o.config.AdminUser, roomID, desired)
}

// JoinRoom joins as the user described by member (empty ActorToken → admin).
func (o *LegacyClientOps) JoinRoom(ctx context.Context, roomID string, member MemberSpec) error {
	return joinRoomForMember(ctx, o.client, roomID, member, o.adminToken)
}

// LeaveRoom leaves as the user described by member (empty ActorToken → admin).
func (o *LegacyClientOps) LeaveRoom(ctx context.Context, roomID string, member MemberSpec) error {
	return leaveRoomForMember(ctx, o.client, roomID, member, o.adminToken)
}

// ForceLeaveAllRooms makes the member leave every joined room. Best-effort.
func (o *LegacyClientOps) ForceLeaveAllRooms(ctx context.Context, member MemberSpec) error {
	return forceLeaveAllRoomsForMember(ctx, o.client, member, o.adminToken)
}

// ReleaseRoomAlias removes a full alias. Idempotent (missing alias → nil).
func (o *LegacyClientOps) ReleaseRoomAlias(ctx context.Context, alias string) error {
	return o.client.DeleteRoomAlias(ctx, alias)
}

// ResolveRoomAlias resolves a full alias to its room ID.
func (o *LegacyClientOps) ResolveRoomAlias(ctx context.Context, alias string) (string, bool, error) {
	return o.client.ResolveRoomAlias(ctx, alias)
}

// ArchiveRoom renames the room via SetRoomName with member.ActorToken
// (empty → admin identity).
func (o *LegacyClientOps) ArchiveRoom(ctx context.Context, roomID, name string, member MemberSpec) error {
	return o.client.SetRoomName(ctx, roomID, name, member.ActorToken)
}

// === Phase 3: Room Metadata & Messaging + Queries & Ops ===

// SetRoomMetadata writes the room.meta state event via SetRoomState using
// member.ActorToken (empty → admin identity).
func (o *LegacyClientOps) SetRoomMetadata(ctx context.Context, roomID string, content map[string]interface{}, member MemberSpec) error {
	return o.client.SetRoomState(ctx, roomID, roomMetaEventType, "", content, member.ActorToken)
}

// RenameRoom renames via SetRoomName with member.ActorToken (empty → admin).
func (o *LegacyClientOps) RenameRoom(ctx context.Context, roomID, name string, member MemberSpec) error {
	return o.client.SetRoomName(ctx, roomID, name, member.ActorToken)
}

// SendSystemMessage sends body to roomID as the admin identity.
func (o *LegacyClientOps) SendSystemMessage(ctx context.Context, roomID, body string) error {
	return o.client.SendMessageAsAdmin(ctx, roomID, body)
}

// ListRoomMembers lists members as member.ActorToken (empty → admin).
func (o *LegacyClientOps) ListRoomMembers(ctx context.Context, roomID string, member MemberSpec) ([]RoomMember, error) {
	return listRoomMembersForMember(ctx, o.client, roomID, member)
}

// ListJoinedRooms lists the rooms the user described by member is joined to
// (empty ActorToken → admin identity).
func (o *LegacyClientOps) ListJoinedRooms(ctx context.Context, member MemberSpec) ([]string, error) {
	token, err := memberTokenFor(ctx, member, o.adminToken)
	if err != nil {
		return nil, fmt.Errorf("list joined rooms: %w", err)
	}
	return o.client.ListJoinedRooms(ctx, token)
}

// IsUserInRoom reports whether userID is currently joined to roomID.
func (o *LegacyClientOps) IsUserInRoom(ctx context.Context, roomID, userID string) (bool, error) {
	return isUserInRoomForMember(ctx, o.client, roomID, userID)
}

// IsManagerJoinedDM reports whether the Manager's user is joined to roomID.
func (o *LegacyClientOps) IsManagerJoinedDM(ctx context.Context, roomID string) (bool, error) {
	return isManagerJoinedDMForMember(ctx, o.client, roomID)
}

// HealthCheck probes homeserver reachability with a deliberately-invalid
// login; any HTTP-level response means the server is up.
func (o *LegacyClientOps) HealthCheck(ctx context.Context) error {
	_, err := o.client.Login(ctx, "__healthcheck__", "invalid")
	if err != nil && isMatrixConnError(err) {
		return err
	}
	return nil
}

// === Phase 4: User Identity & Credentials + AppService Governance ===
//
// The legacy bridge forwards every Phase 4 operation straight to the wrapped
// protocol-level client (Tuwunel semantics), identical to the pre-abstraction
// service layer.

// ProvisionUser forwards to client.EnsureUser (password-mode provisioning).
func (o *LegacyClientOps) ProvisionUser(ctx context.Context, spec UserSpec) (*UserRef, *UserCredentials, error) {
	uc, err := o.client.EnsureUser(ctx, EnsureUserRequest{Username: spec.Username, Password: spec.Password})
	if err != nil {
		return nil, nil, err
	}
	return &UserRef{UserID: uc.UserID, Created: uc.Created}, uc, nil
}

// ProvisionUserViaAppService forwards to client.EnsureAppServiceUser.
func (o *LegacyClientOps) ProvisionUserViaAppService(ctx context.Context, localpart string) (*UserRef, *UserCredentials, error) {
	uc, err := o.client.EnsureAppServiceUser(ctx, localpart)
	if err != nil {
		return nil, nil, err
	}
	return &UserRef{UserID: uc.UserID, Created: uc.Created}, uc, nil
}

// LoginUser forwards to client.Login.
func (o *LegacyClientOps) LoginUser(ctx context.Context, username, password string) (string, error) {
	return o.client.Login(ctx, username, password)
}

// LoginUserViaAppService forwards to client.LoginAppServiceUser.
func (o *LegacyClientOps) LoginUserViaAppService(ctx context.Context, localpart string) (string, error) {
	return o.client.LoginAppServiceUser(ctx, localpart)
}

// ResetUserPassword forwards to client.SetPasswordAsAdmin.
func (o *LegacyClientOps) ResetUserPassword(ctx context.Context, userID, password string) error {
	return o.client.SetPasswordAsAdmin(ctx, userID, password)
}

// DeactivateUser sends the fire-and-forget `!admin users deactivate <userID>`
// command to the Tuwunel admin bot (same command the service layer's
// deactivateHumanUser sent).
func (o *LegacyClientOps) DeactivateUser(ctx context.Context, userID string) error {
	cmd := fmt.Sprintf("!admin users deactivate %s", userID)
	return o.client.AdminCommand(ctx, cmd)
}

// SetUserDisplayName forwards to client.SetDisplayName.
func (o *LegacyClientOps) SetUserDisplayName(ctx context.Context, userID, accessToken, displayName string) error {
	return o.client.SetDisplayName(ctx, userID, accessToken, displayName)
}

// VerifyUserAccessToken forwards to client.VerifyAccessToken.
func (o *LegacyClientOps) VerifyUserAccessToken(ctx context.Context, accessToken string) error {
	return o.client.VerifyAccessToken(ctx, accessToken)
}

// UserIDFor forwards to client.UserID (pure formatting).
func (o *LegacyClientOps) UserIDFor(localpart string) string {
	return o.client.UserID(localpart)
}

// BackfillLegacyPassword forwards to client.SetPasswordAsAdmin (same
// underlying op as ResetUserPassword, distinct migration intent).
func (o *LegacyClientOps) BackfillLegacyPassword(ctx context.Context, userID, password string) error {
	return o.client.SetPasswordAsAdmin(ctx, userID, password)
}

// RegisterAppService forwards to client.RegisterAppService.
func (o *LegacyClientOps) RegisterAppService(ctx context.Context, reg AppServiceRegistration) error {
	return o.client.RegisterAppService(ctx, reg)
}

// UnregisterAppService forwards to client.UnregisterAppService.
func (o *LegacyClientOps) UnregisterAppService(ctx context.Context, id string) error {
	return o.client.UnregisterAppService(ctx, id)
}

// SmokeTestAppService forwards to client.AppServiceSmokeTest.
func (o *LegacyClientOps) SmokeTestAppService(ctx context.Context) error {
	return o.client.AppServiceSmokeTest(ctx)
}

var _ MatrixOps = (*LegacyClientOps)(nil)
