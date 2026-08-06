package matrix

import (
	"context"
	"fmt"
	"net/http"
	"strings"
)

// roomMetaEventType is the custom state event type the controller uses to
// persist business room metadata (schemaVersion / roomKind / lifecycle /
// createdBy plus room-specific fields). Shared by every MatrixOps
// implementation and the service layer's roomMeta helpers.
const roomMetaEventType = "room.meta"

// TuwunelMatrixOps implements MatrixOps against a Tuwunel homeserver.
//
// Admin operations go through the Tuwunel admin bot ("!admin ...") delivered
// via the embedded *TuwunelClient; CS API operations (createRoom, invite,
// kick, ...) reuse the client verbatim. This is the zero-regression reference
// implementation: its behavior is intentionally identical to the pre-
// abstraction service layer, so existing tests double as its contract.
type TuwunelMatrixOps struct {
	*TuwunelClient
	config Config
}

// NewTuwunelMatrixOps creates a Tuwunel-backed MatrixOps implementation.
func NewTuwunelMatrixOps(cfg Config, httpClient *http.Client) *TuwunelMatrixOps {
	return &TuwunelMatrixOps{
		TuwunelClient: NewTuwunelClient(cfg, httpClient),
		config:        cfg,
	}
}

// CreateRoom implements MatrixOps.CreateRoom for Tuwunel by translating the
// business RoomSpec into a protocol CreateRoomRequest and delegating to the
// embedded client. CreatorToken is left empty so the client resolves the
// admin token (the ops layer never needs to know whose token creates the
// room). Alias idempotency is preserved: when the alias already exists the
// client resolves it and returns RoomRef{Created: false}.
func (o *TuwunelMatrixOps) CreateRoom(ctx context.Context, spec RoomSpec) (*RoomRef, error) {
	info, err := o.TuwunelClient.CreateRoom(ctx, roomSpecToRequest(spec))
	if err != nil {
		return nil, err
	}
	return &RoomRef{RoomID: info.RoomID, Created: info.Created}, nil
}

// DissolveRoom implements MatrixOps.DissolveRoom for Tuwunel by sending the
// fire-and-forget `!admin rooms delete-room <roomID>` command to the admin
// bot. Tuwunel processes it asynchronously; the
// delete_rooms_after_leave/forget_forced_upon_leave homeserver settings act
// as a fallback if this never lands.
func (o *TuwunelMatrixOps) DissolveRoom(ctx context.Context, roomID string) error {
	if roomID == "" {
		return nil
	}
	cmd := fmt.Sprintf("!admin rooms delete-room %s", roomID)
	return o.TuwunelClient.AdminCommand(ctx, cmd)
}

// AddMember implements MatrixOps.AddMember for Tuwunel by inviting via the
// admin token. Idempotent: returns nil when the user is already joined or
// invited (handled inside InviteToRoom).
func (o *TuwunelMatrixOps) AddMember(ctx context.Context, roomID, userID string) error {
	return o.TuwunelClient.InviteToRoom(ctx, roomID, userID)
}

// InviteMember implements MatrixOps.InviteMember for Tuwunel. When
// member.ActorToken is set the invite is issued as that user (e.g. a team
// leader already joined to the DM room); otherwise it falls back to the
// admin identity via AddMember.
func (o *TuwunelMatrixOps) InviteMember(ctx context.Context, roomID, userID string, member MemberSpec) error {
	if member.ActorToken != "" {
		return o.TuwunelClient.InviteToRoomWithToken(ctx, roomID, userID, member.ActorToken)
	}
	return o.AddMember(ctx, roomID, userID)
}

// RemoveMember implements MatrixOps.RemoveMember for Tuwunel. It first tries
// the CS API kick with the admin token (idempotent when the target is not in
// the room). When the kick fails because the operator lacks power (or is not
// joined), it escalates to the admin bot `!admin users force-leave-room
// <userID> <roomID>` command. The command delivery is fire-and-forget; a
// successful delivery is treated as success (the bot executes it
// asynchronously).
func (o *TuwunelMatrixOps) RemoveMember(ctx context.Context, roomID, userID, reason string) error {
	err := o.TuwunelClient.KickFromRoom(ctx, roomID, userID, reason)
	if err == nil {
		return nil
	}
	if !shouldForceLeaveAfterKickError(err) {
		return err
	}
	cmd := fmt.Sprintf("!admin users force-leave-room %s %s", userID, roomID)
	if cmdErr := o.TuwunelClient.AdminCommand(ctx, cmd); cmdErr != nil {
		return fmt.Errorf("kick %s from %s failed (%v) and force-leave-room command failed: %w",
			userID, roomID, err, cmdErr)
	}
	return nil
}

// ReconcileMembers implements MatrixOps.ReconcileMembers for Tuwunel. The
// full convergence logic is shared with SynapseMatrixOps (same CS API
// surface) — see reconcileMembersImpl. With no ActorToken the admin identity
// drives listing/invites/kicks with provider-specific fallbacks (admin bot
// force-leave); with an ActorToken the CS WithToken methods are used, and a
// kick that fails on power escalates to the admin RemoveMember once.
func (o *TuwunelMatrixOps) ReconcileMembers(ctx context.Context, roomID string, desired []MemberSpec) error {
	return reconcileMembersImpl(ctx, o, o.TuwunelClient, o.config.AdminUser, roomID, desired)
}

// JoinRoom implements MatrixOps.JoinRoom for Tuwunel by joining as the user
// described by member (member.ActorToken, empty → admin identity).
func (o *TuwunelMatrixOps) JoinRoom(ctx context.Context, roomID string, member MemberSpec) error {
	return joinRoomForMember(ctx, o.TuwunelClient, roomID, member, o.ensureAdminToken)
}

// LeaveRoom implements MatrixOps.LeaveRoom for Tuwunel by leaving as the user
// described by member (member.ActorToken, empty → admin identity).
func (o *TuwunelMatrixOps) LeaveRoom(ctx context.Context, roomID string, member MemberSpec) error {
	return leaveRoomForMember(ctx, o.TuwunelClient, roomID, member, o.ensureAdminToken)
}

// ForceLeaveAllRooms implements MatrixOps.ForceLeaveAllRooms for Tuwunel.
// Best-effort: errors leaving individual rooms are logged, not returned.
func (o *TuwunelMatrixOps) ForceLeaveAllRooms(ctx context.Context, member MemberSpec) error {
	return forceLeaveAllRoomsForMember(ctx, o.TuwunelClient, member, o.ensureAdminToken)
}

// ReleaseRoomAlias implements MatrixOps.ReleaseRoomAlias for Tuwunel.
// Idempotent: a missing alias returns nil (handled inside DeleteRoomAlias).
func (o *TuwunelMatrixOps) ReleaseRoomAlias(ctx context.Context, alias string) error {
	return o.TuwunelClient.DeleteRoomAlias(ctx, alias)
}

// ResolveRoomAlias implements MatrixOps.ResolveRoomAlias for Tuwunel.
func (o *TuwunelMatrixOps) ResolveRoomAlias(ctx context.Context, alias string) (string, bool, error) {
	return o.TuwunelClient.ResolveRoomAlias(ctx, alias)
}

// ArchiveRoom implements MatrixOps.ArchiveRoom for Tuwunel by renaming the
// room via SetRoomName with member.ActorToken (empty → admin identity).
func (o *TuwunelMatrixOps) ArchiveRoom(ctx context.Context, roomID, name string, member MemberSpec) error {
	return o.TuwunelClient.SetRoomName(ctx, roomID, name, member.ActorToken)
}

// === Phase 3: Room Metadata & Messaging + Queries & Ops ===

// SetRoomMetadata implements MatrixOps.SetRoomMetadata for Tuwunel by writing
// the room.meta state event as the user described by member (member.ActorToken,
// empty → admin identity). No fallback needed: the admin/team-admin token
// already has power in the room it writes to (see design/synapse-support.md
// §3.4 D1).
func (o *TuwunelMatrixOps) SetRoomMetadata(ctx context.Context, roomID string, content map[string]interface{}, member MemberSpec) error {
	return o.TuwunelClient.SetRoomState(ctx, roomID, roomMetaEventType, "", content, member.ActorToken)
}

// RenameRoom implements MatrixOps.RenameRoom for Tuwunel by renaming via
// SetRoomName as the user described by member (empty → admin identity).
func (o *TuwunelMatrixOps) RenameRoom(ctx context.Context, roomID, name string, member MemberSpec) error {
	return o.TuwunelClient.SetRoomName(ctx, roomID, name, member.ActorToken)
}

// SendSystemMessage implements MatrixOps.SendSystemMessage for Tuwunel by
// sending the plain-text body as the admin identity (SendMessageAsAdmin).
func (o *TuwunelMatrixOps) SendSystemMessage(ctx context.Context, roomID, body string) error {
	return o.TuwunelClient.SendMessageAsAdmin(ctx, roomID, body)
}

// ListRoomMembers implements MatrixOps.ListRoomMembers for Tuwunel. When
// member.ActorToken is set the read uses that token (the user must be allowed
// to read room state); otherwise it uses the admin identity.
func (o *TuwunelMatrixOps) ListRoomMembers(ctx context.Context, roomID string, member MemberSpec) ([]RoomMember, error) {
	return listRoomMembersForMember(ctx, o.TuwunelClient, roomID, member)
}

// ListJoinedRooms implements MatrixOps.ListJoinedRooms for Tuwunel, listing
// the rooms the user described by member is joined to (empty ActorToken →
// admin identity).
func (o *TuwunelMatrixOps) ListJoinedRooms(ctx context.Context, member MemberSpec) ([]string, error) {
	token, err := memberTokenFor(ctx, member, o.ensureAdminToken)
	if err != nil {
		return nil, fmt.Errorf("list joined rooms: %w", err)
	}
	return o.TuwunelClient.ListJoinedRooms(ctx, token)
}

// IsUserInRoom implements MatrixOps.IsUserInRoom for Tuwunel. Pure read via
// the admin identity (bypasses in-room checks).
func (o *TuwunelMatrixOps) IsUserInRoom(ctx context.Context, roomID, userID string) (bool, error) {
	return isUserInRoomForMember(ctx, o.TuwunelClient, roomID, userID)
}

// IsManagerJoinedDM implements MatrixOps.IsManagerJoinedDM for Tuwunel. Pure
// read via the admin identity; safe to poll on every reconcile.
func (o *TuwunelMatrixOps) IsManagerJoinedDM(ctx context.Context, roomID string) (bool, error) {
	return isManagerJoinedDMForMember(ctx, o.TuwunelClient, roomID)
}

// HealthCheck implements MatrixOps.HealthCheck for Tuwunel by attempting a
// login with deliberately-invalid credentials. Any HTTP-level response (401,
// 403, ...) means the server is up; a transport error (connection refused,
// DNS failure, EOF) is returned.
func (o *TuwunelMatrixOps) HealthCheck(ctx context.Context) error {
	_, err := o.TuwunelClient.Login(ctx, "__healthcheck__", "invalid")
	if err != nil && isMatrixConnError(err) {
		return err
	}
	return nil
}

// shouldForceLeaveAfterKickError reports whether a kick failure should be
// escalated to the admin force-leave-room path. Matches both Tuwunel-style
// ("not have enough power") and Synapse 1.127 error strings ("cannot kick
// user" / "cannot unban user" / "not in room") so the same escalation logic
// stays provider-agnostic (see design/synapse-interface-contracts.md §1 修复 2).
func shouldForceLeaveAfterKickError(err error) bool {
	if err == nil {
		return false
	}
	msg := strings.ToLower(err.Error())
	if !strings.Contains(msg, "m_forbidden") {
		return false
	}
	// Tuwunel style.
	if strings.Contains(msg, "not have enough power") || strings.Contains(msg, "power") {
		return true
	}
	// Synapse 1.127 style (event_auth.py:717 — PL insufficient).
	if strings.Contains(msg, "cannot kick user") || strings.Contains(msg, "cannot unban user") {
		return true
	}
	// Synapse 1.127 style (event_auth.py:687 — sender not joined).
	if strings.Contains(msg, "not in room") {
		return true
	}
	return false
}

// ---------------------------------------------------------------------------
// Shared RoomSpec translation (used by both TuwunelMatrixOps and
// SynapseMatrixOps)
// ---------------------------------------------------------------------------

// roomSpecToRequest translates a business RoomSpec into the protocol-level
// CreateRoomRequest consumed by the underlying client. CreatorToken comes
// from spec.ActorToken (empty → the client resolves the admin token), so the
// team room / leader DM can be created as the team admin exactly as the
// pre-abstraction service layer did.
func roomSpecToRequest(spec RoomSpec) CreateRoomRequest {
	req := CreateRoomRequest{
		Name:          spec.Name,
		Topic:         spec.Topic,
		Invite:        spec.Invite,
		PowerLevels:   spec.PowerLevels,
		IsDirect:      spec.IsDirect,
		E2EE:          spec.E2EE,
		InitialState:  spec.InitialState,
		RoomAliasName: spec.AliasLocalpart,
		CreatorToken:  spec.ActorToken,
	}
	if meta := roomMetadataState(spec.Metadata); len(meta) > 0 {
		req.InitialState = append(req.InitialState, meta...)
	}
	return req
}

// roomMetadataState renders RoomMetadata as a room.meta state event for
// createRoom.initial_state, matching the shape the service layer produced via
// baseRoomMeta (schemaVersion / roomKind / lifecycle / createdBy) plus any
// extra fields.
func roomMetadataState(m *RoomMetadata) []StateEvent {
	if m == nil {
		return nil
	}
	content := map[string]interface{}{
		"schemaVersion": m.SchemaVersion,
		"roomKind":      m.Kind,
		"lifecycle":     "persistent",
		"createdBy":     "agentteams",
	}
	if m.SchemaVersion == 0 {
		content["schemaVersion"] = 1
	}
	for k, v := range m.Extra {
		content[k] = v
	}
	return []StateEvent{{
		Type:     "room.meta",
		StateKey: "",
		Content:  content,
	}}
}

// === Phase 4: User Identity & Credentials + AppService Governance ===

// ProvisionUser implements MatrixOps.ProvisionUser for Tuwunel by delegating
// to the client's EnsureUser (registration-token register with login and
// orphan-recovery fallback). An empty spec.Password yields a generated one,
// which is returned in the UserCredentials.
func (o *TuwunelMatrixOps) ProvisionUser(ctx context.Context, spec UserSpec) (*UserRef, *UserCredentials, error) {
	uc, err := o.TuwunelClient.EnsureUser(ctx, EnsureUserRequest{Username: spec.Username, Password: spec.Password})
	if err != nil {
		return nil, nil, err
	}
	return &UserRef{UserID: uc.UserID, Created: uc.Created}, uc, nil
}

// ProvisionUserViaAppService implements MatrixOps.ProvisionUserViaAppService
// for Tuwunel via the client's EnsureAppServiceUser (as_token register with
// login fallback). The returned credentials carry an empty Password.
func (o *TuwunelMatrixOps) ProvisionUserViaAppService(ctx context.Context, localpart string) (*UserRef, *UserCredentials, error) {
	uc, err := o.TuwunelClient.EnsureAppServiceUser(ctx, localpart)
	if err != nil {
		return nil, nil, err
	}
	return &UserRef{UserID: uc.UserID, Created: uc.Created}, uc, nil
}

// LoginUser implements MatrixOps.LoginUser for Tuwunel via password login.
func (o *TuwunelMatrixOps) LoginUser(ctx context.Context, username, password string) (string, error) {
	return o.TuwunelClient.Login(ctx, username, password)
}

// LoginUserViaAppService implements MatrixOps.LoginUserViaAppService for
// Tuwunel via the AS login flow (m.login.application_service).
func (o *TuwunelMatrixOps) LoginUserViaAppService(ctx context.Context, localpart string) (string, error) {
	return o.TuwunelClient.LoginAppServiceUser(ctx, localpart)
}

// ResetUserPassword implements MatrixOps.ResetUserPassword for Tuwunel via the
// admin bot "!admin users reset-password" command.
func (o *TuwunelMatrixOps) ResetUserPassword(ctx context.Context, userID, password string) error {
	return o.TuwunelClient.SetPasswordAsAdmin(ctx, userID, password)
}

// DeactivateUser implements MatrixOps.DeactivateUser for Tuwunel via the admin
// bot "!admin users deactivate" command. Fire-and-forget: delivery of the
// command is confirmed but execution of the deactivation is not.
func (o *TuwunelMatrixOps) DeactivateUser(ctx context.Context, userID string) error {
	cmd := fmt.Sprintf("!admin users deactivate %s", userID)
	return o.TuwunelClient.AdminCommand(ctx, cmd)
}

// SetUserDisplayName implements MatrixOps.SetUserDisplayName for Tuwunel. When
// accessToken is empty the admin identity is resolved and used.
func (o *TuwunelMatrixOps) SetUserDisplayName(ctx context.Context, userID, accessToken, displayName string) error {
	if accessToken == "" {
		var err error
		accessToken, err = o.ensureAdminToken(ctx)
		if err != nil {
			return fmt.Errorf("set displayName for %s: %w", userID, err)
		}
	}
	return o.TuwunelClient.SetDisplayName(ctx, userID, accessToken, displayName)
}

// VerifyUserAccessToken implements MatrixOps.VerifyUserAccessToken for Tuwunel
// via GET /_matrix/client/v3/account/whoami.
func (o *TuwunelMatrixOps) VerifyUserAccessToken(ctx context.Context, accessToken string) error {
	return o.TuwunelClient.VerifyAccessToken(ctx, accessToken)
}

// UserIDFor implements MatrixOps.UserIDFor for Tuwunel: pure formatting of
// "@<localpart>:<domain>".
func (o *TuwunelMatrixOps) UserIDFor(localpart string) string {
	return o.TuwunelClient.UserID(localpart)
}

// BackfillLegacyPassword implements MatrixOps.BackfillLegacyPassword for
// Tuwunel. Same underlying admin operation as ResetUserPassword (the client's
// SetPasswordAsAdmin), kept separate to signal the bulk-migration intent.
func (o *TuwunelMatrixOps) BackfillLegacyPassword(ctx context.Context, userID, password string) error {
	return o.TuwunelClient.SetPasswordAsAdmin(ctx, userID, password)
}

// RegisterAppService implements MatrixOps.RegisterAppService for Tuwunel by
// delegating to the client's smoke-test-first, unregister-before-register
// admin bot registration.
func (o *TuwunelMatrixOps) RegisterAppService(ctx context.Context, reg AppServiceRegistration) error {
	return o.TuwunelClient.RegisterAppService(ctx, reg)
}

// UnregisterAppService implements MatrixOps.UnregisterAppService for Tuwunel
// via the admin bot command.
func (o *TuwunelMatrixOps) UnregisterAppService(ctx context.Context, id string) error {
	return o.TuwunelClient.UnregisterAppService(ctx, id)
}

// SmokeTestAppService implements MatrixOps.SmokeTestAppService for Tuwunel via
// an AS login as the sender_localpart user.
func (o *TuwunelMatrixOps) SmokeTestAppService(ctx context.Context) error {
	return o.TuwunelClient.AppServiceSmokeTest(ctx)
}
