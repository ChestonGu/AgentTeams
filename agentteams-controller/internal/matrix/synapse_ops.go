package matrix

import (
	"context"
	"fmt"
	"net/http"
	"strings"
)

// SynapseMatrixOps implements MatrixOps against a Synapse homeserver (1.127).
//
// CS API operations (createRoom, invite, kick) reuse the embedded *matrixClient
// — Synapse implements the same /_matrix/client/v3/* surface, so the standard
// client methods need no change. Admin operations go through the Synapse REST
// admin API (/_synapse/admin/v1/*) via the synapseAdmin *SynapseClient. The
// matrixClient and synapseAdmin share the same underlying *matrixClient
// instance, so the cached admin access token is shared between CS API calls
// and admin REST calls.
//
// SynapseMatrixOps NEVER calls Tuwunel-specific methods (AdminCommand,
// SendMessageAsAdmin, SetPasswordAsAdmin, EnsureUser registration_token flow,
// runtime AppService register/unregister). System messages are sent via the
// private sendMessageAsAdmin helper (ensureAdminToken + SendMessage), user
// provisioning via synapseAdmin.EnsureUser (admin REST), AppService governance
// via smoke-test-only (declarative).
//
// The in-room fallback strategy differs from TuwunelMatrixOps and is composed
// from Synapse's own admin capabilities, driven by which sender-side check
// rejected the CS operation (event_auth.py, see
// design/synapse-interface-contracts.md):
//   - sender not joined ("... not in room ...") → force-join the sender via
//     POST /_synapse/admin/v1/join/{roomID} (the native membership-restore
//     endpoint: auto-invite + join), then retry the CS op.
//   - sender lacks power ("permission to invite/post", "cannot kick user") →
//     make_room_admin (raises the sender to the room's highest admin power
//     level), then retry the CS op.
//
// Synapse 1.127 has no admin kick endpoint, so the kick path still goes
// through the CS API after recovery.
type SynapseMatrixOps struct {
	*matrixClient
	synapseAdmin *SynapseClient
	config       Config
}

// NewSynapseMatrixOps creates a Synapse-backed MatrixOps implementation.
func NewSynapseMatrixOps(cfg Config, httpClient *http.Client) *SynapseMatrixOps {
	base := newMatrixClient(cfg, httpClient)
	return &SynapseMatrixOps{
		matrixClient: base,
		synapseAdmin: &SynapseClient{matrixClient: base},
		config:       cfg,
	}
}

// CreateRoom implements MatrixOps.CreateRoom for Synapse by translating the
// business RoomSpec into a protocol CreateRoomRequest and delegating to the
// embedded client (creator token from spec.ActorToken, empty → admin token;
// alias idempotency preserved).
//
// Synapse 1.127 additionally rejects a power_level_content_override whose
// "users" map omits the room creator (HTTP 400 M_BAD_JSON,
// synapse/handlers/room.py:878-888). The creator is the user whose token
// authenticates the request — spec.ActorUserID when set (e.g. a team admin
// owning the team room), otherwise the admin user — so the ops layer
// guarantees that creator is present with PL=100
// (design/synapse-interface-contracts.md §4 修复 4). When the business layer
// already put the actor in PowerLevels (the team room always does), this is
// a no-op.
func (o *SynapseMatrixOps) CreateRoom(ctx context.Context, spec RoomSpec) (*RoomRef, error) {
	req := roomSpecToRequest(spec)
	if len(req.PowerLevels) > 0 {
		creator := spec.ActorUserID
		if creator == "" {
			creator = o.UserID(o.config.AdminUser)
		}
		if _, ok := req.PowerLevels[creator]; !ok {
			req.PowerLevels[creator] = 100
		}
	}
	info, err := o.matrixClient.CreateRoom(ctx, req)
	if err != nil {
		return nil, err
	}
	return &RoomRef{RoomID: info.RoomID, Created: info.Created}, nil
}

// DissolveRoom implements MatrixOps.DissolveRoom for Synapse via the admin
// REST API: DELETE /_synapse/admin/v2/rooms/{roomID} shuts down and purges
// the room (fire-and-forget — the purge runs asynchronously inside Synapse).
func (o *SynapseMatrixOps) DissolveRoom(ctx context.Context, roomID string) error {
	if roomID == "" {
		return nil
	}
	return o.synapseAdmin.DeleteRoom(ctx, roomID)
}

// AddMember implements MatrixOps.AddMember for Synapse. It first invites via
// the admin token (idempotent when the target is already joined/invited —
// handled inside InviteToRoom). When the invite fails because the admin is not
// joined ("@admin not in room", event_auth.py:687) or lacks power ("You don't
// have permission to invite users", event_auth.py:703), the failure is
// classified and recovered with Synapse's own capabilities: force-join the
// admin via the admin API (POST /_synapse/admin/v1/join) when not joined, or
// make_room_admin when they lack power, then retry the CS invite.
func (o *SynapseMatrixOps) AddMember(ctx context.Context, roomID, userID string) error {
	err := o.matrixClient.InviteToRoom(ctx, roomID, userID)
	if err == nil {
		return nil
	}
	return o.recoverSynapseSenderOp(ctx, roomID, "invite "+userID+" to", o.UserID(o.config.AdminUser), func() error {
		return o.matrixClient.InviteToRoom(ctx, roomID, userID)
	}, err)
}

// InviteMember implements MatrixOps.InviteMember for Synapse. When
// member.ActorToken is set the invite is issued as that user (e.g. a team
// leader already joined to the DM room, who may lack power to invite via the
// admin identity); otherwise it falls back to AddMember (admin invite with
// provider-specific recovery when the admin is not joined or lacks PL).
func (o *SynapseMatrixOps) InviteMember(ctx context.Context, roomID, userID string, member MemberSpec) error {
	if member.ActorToken != "" {
		return o.matrixClient.InviteToRoomWithToken(ctx, roomID, userID, member.ActorToken)
	}
	return o.AddMember(ctx, roomID, userID)
}

// RemoveMember implements MatrixOps.RemoveMember for Synapse. It first kicks
// via the admin token (idempotent when the target is not in the room —
// handled inside KickFromRoom). When the kick fails because the admin is not
// joined or lacks power, the failure is classified and recovered with
// Synapse's own capabilities: force-join the admin via the admin API when not
// joined, or make_room_admin when they lack power, then retry the CS kick. It
// deliberately does NOT call the non-existent Synapse admin kick endpoint
// (/_synapse/admin/v1/rooms/{id}/kick — 404 on Synapse 1.127, see
// design/synapse-interface-contracts.md §3).
func (o *SynapseMatrixOps) RemoveMember(ctx context.Context, roomID, userID, reason string) error {
	err := o.matrixClient.KickFromRoom(ctx, roomID, userID, reason)
	if err == nil {
		return nil
	}
	return o.recoverSynapseSenderOp(ctx, roomID, "kick "+userID+" from", o.UserID(o.config.AdminUser), func() error {
		return o.matrixClient.KickFromRoom(ctx, roomID, userID, reason)
	}, err)
}

// synapseSenderRecovery classifies a failed CS operation's sender-side
// rejection into the Synapse-native recovery that addresses it.
type synapseSenderRecovery int

const (
	// synapseRecoveryNone: no recovery applies — return the error unchanged.
	synapseRecoveryNone synapseSenderRecovery = iota
	// synapseRecoveryJoin: the sender is not joined to the room (event_auth.py
	// sender-membership check). Recover by force-joining the sender via the
	// admin API (POST /_synapse/admin/v1/join/{roomID}).
	synapseRecoveryJoin
	// synapseRecoveryPower: the sender is joined but lacks the power level the
	// operation requires (event_auth.py power checks). Recover by raising the
	// sender via make_room_admin.
	synapseRecoveryPower
)

// classifySynapseSenderError maps a failed CS operation's error to the
// Synapse-native recovery that addresses the sender-side rejection. Errors
// that are nil, not M_FORBIDDEN, or not a sender-membership/power rejection
// map to synapseRecoveryNone — idempotent already-joined/already-invited
// cases return nil inside the CS client before an error reaches here.
func classifySynapseSenderError(err error) synapseSenderRecovery {
	if err == nil {
		return synapseRecoveryNone
	}
	msg := strings.ToLower(err.Error())
	if !strings.Contains(msg, "m_forbidden") {
		return synapseRecoveryNone
	}
	// Sender not joined (event_auth.py:687 / :731). Note: this deliberately
	// does NOT match the target-not-in-room idempotent string ("The target
	// user is not in the room" — the "the" separates the tokens) which
	// KickFromRoom already returns nil for.
	if strings.Contains(msg, "not in room") {
		return synapseRecoveryJoin
	}
	// Insufficient power (event_auth.py:703 / :717 / :768).
	if strings.Contains(msg, "permission to invite") ||
		strings.Contains(msg, "permission to post") ||
		strings.Contains(msg, "cannot kick user") ||
		strings.Contains(msg, "cannot unban user") {
		return synapseRecoveryPower
	}
	return synapseRecoveryNone
}

// recoverSynapseSenderOp recovers a CS operation that failed with a
// sender-side rejection and retries it, composing Synapse's own admin
// capabilities based on the classified failure:
//
//   - synapseRecoveryJoin (sender not joined): force-join the sender via
//     POST /_synapse/admin/v1/join/{roomID} — the native membership-restore
//     endpoint (auto-invite + join). The retry then succeeds when the
//     sender's power level survives in m.room.power_levels (e.g. a
//     previously-joined room admin); otherwise it fails again with an
//     insufficient-power error and falls through to the power recovery.
//   - synapseRecoveryPower (insufficient power): make_room_admin the sender
//     (raises them to the room's highest admin power level), then retry.
//
// Any other error is returned unchanged. `op` names the failed operation for
// error wrapping (e.g. "invite @alice:d to", "kick @alice:d from").
func (o *SynapseMatrixOps) recoverSynapseSenderOp(ctx context.Context, roomID, op, senderID string, retry func() error, err error) error {
	recovery := classifySynapseSenderError(err)
	if recovery == synapseRecoveryJoin {
		if joinErr := o.synapseAdmin.ForceJoinRoom(ctx, roomID, senderID); joinErr != nil {
			return fmt.Errorf("%s %s failed (%v) and force-join failed: %w", op, roomID, err, joinErr)
		}
		retryErr := retry()
		if retryErr == nil {
			return nil
		}
		if classifySynapseSenderError(retryErr) != synapseRecoveryPower {
			return retryErr
		}
		err = retryErr
		recovery = synapseRecoveryPower
	}
	if recovery != synapseRecoveryPower {
		return err
	}
	if adminErr := o.synapseAdmin.MakeRoomAdmin(ctx, roomID, senderID); adminErr != nil {
		return fmt.Errorf("%s %s failed (%v) and make_room_admin failed: %w", op, roomID, err, adminErr)
	}
	return retry()
}

// === Phase 2: Room Lifecycle (alias) + Room Membership (full) ===

// ReconcileMembers implements MatrixOps.ReconcileMembers for Synapse. It
// shares the exact convergence core with TuwunelMatrixOps (same CS API
// surface) — see reconcileMembersImpl. The admin path uses the ops layer's
// provider-specific RemoveMember (force-join / make_room_admin + retry); the
// actor-token path uses the CS WithToken methods and escalates a failing kick
// once via RemoveMember.
func (o *SynapseMatrixOps) ReconcileMembers(ctx context.Context, roomID string, desired []MemberSpec) error {
	return reconcileMembersImpl(ctx, o, o.matrixClient, o.config.AdminUser, roomID, desired)
}

// JoinRoom implements MatrixOps.JoinRoom for Synapse by joining via the CS
// API as the user described by member (empty ActorToken → admin identity).
func (o *SynapseMatrixOps) JoinRoom(ctx context.Context, roomID string, member MemberSpec) error {
	return joinRoomForMember(ctx, o.matrixClient, roomID, member, o.ensureAdminToken)
}

// LeaveRoom implements MatrixOps.LeaveRoom for Synapse by leaving via the CS
// API as the user described by member (empty ActorToken → admin identity).
func (o *SynapseMatrixOps) LeaveRoom(ctx context.Context, roomID string, member MemberSpec) error {
	return leaveRoomForMember(ctx, o.matrixClient, roomID, member, o.ensureAdminToken)
}

// ForceLeaveAllRooms implements MatrixOps.ForceLeaveAllRooms for Synapse.
// Best-effort: errors leaving individual rooms are logged, not returned.
func (o *SynapseMatrixOps) ForceLeaveAllRooms(ctx context.Context, member MemberSpec) error {
	return forceLeaveAllRoomsForMember(ctx, o.matrixClient, member, o.ensureAdminToken)
}

// ReleaseRoomAlias implements MatrixOps.ReleaseRoomAlias for Synapse.
// Idempotent: a missing alias returns nil (handled inside DeleteRoomAlias).
func (o *SynapseMatrixOps) ReleaseRoomAlias(ctx context.Context, alias string) error {
	return o.matrixClient.DeleteRoomAlias(ctx, alias)
}

// ResolveRoomAlias implements MatrixOps.ResolveRoomAlias for Synapse.
func (o *SynapseMatrixOps) ResolveRoomAlias(ctx context.Context, alias string) (string, bool, error) {
	return o.matrixClient.ResolveRoomAlias(ctx, alias)
}

// ArchiveRoom implements MatrixOps.ArchiveRoom for Synapse by renaming the
// room via SetRoomName with member.ActorToken (empty → admin identity).
func (o *SynapseMatrixOps) ArchiveRoom(ctx context.Context, roomID, name string, member MemberSpec) error {
	return o.matrixClient.SetRoomName(ctx, roomID, name, member.ActorToken)
}

// === Phase 3: Room Metadata & Messaging + Queries & Ops ===

// SetRoomMetadata implements MatrixOps.SetRoomMetadata for Synapse. It first
// writes room.meta via the CS API as the user described by member (empty
// ActorToken → admin identity). When the write fails because that user is not
// joined or lacks power (PL < state_default=50), the failure is classified
// and recovered with Synapse's own capabilities: force-join the actor via the
// admin API when not joined, or make_room_admin when they lack power, then
// retry the CS write with the same token.
func (o *SynapseMatrixOps) SetRoomMetadata(ctx context.Context, roomID string, content map[string]interface{}, member MemberSpec) error {
	err := o.matrixClient.SetRoomState(ctx, roomID, roomMetaEventType, "", content, member.ActorToken)
	if err == nil {
		return nil
	}
	actorID := member.ActorUserID
	if actorID == "" {
		actorID = o.UserID(o.config.AdminUser)
	}
	return o.recoverSynapseSenderOp(ctx, roomID, "set room meta for", actorID, func() error {
		return o.matrixClient.SetRoomState(ctx, roomID, roomMetaEventType, "", content, member.ActorToken)
	}, err)
}

// RenameRoom implements MatrixOps.RenameRoom for Synapse. Same recovery
// strategy as SetRoomMetadata: CS rename (m.room.name send_level=50), then
// force-join (not joined) or make_room_admin (lacks power) + retry.
func (o *SynapseMatrixOps) RenameRoom(ctx context.Context, roomID, name string, member MemberSpec) error {
	err := o.matrixClient.SetRoomName(ctx, roomID, name, member.ActorToken)
	if err == nil {
		return nil
	}
	actorID := member.ActorUserID
	if actorID == "" {
		actorID = o.UserID(o.config.AdminUser)
	}
	return o.recoverSynapseSenderOp(ctx, roomID, "rename", actorID, func() error {
		return o.matrixClient.SetRoomName(ctx, roomID, name, member.ActorToken)
	}, err)
}

// sendMessageAsAdmin sends a plain-text message to roomID as the admin user.
// It is the Synapse-native equivalent of matrixClient.SendMessageAsAdmin: it
// resolves and caches the admin token (shared with synapseAdmin via the
// embedded *matrixClient) and delegates to the standard CS SendMessage.
// SynapseMatrixOps uses this instead of matrixClient.SendMessageAsAdmin so
// Synapse never depends on Tuwunel-specific code paths.
func (o *SynapseMatrixOps) sendMessageAsAdmin(ctx context.Context, roomID, body string) error {
	token, err := o.ensureAdminToken(ctx)
	if err != nil {
		return fmt.Errorf("send admin message: %w", err)
	}
	return o.matrixClient.SendMessage(ctx, roomID, token, body)
}

// SendSystemMessage implements MatrixOps.SendSystemMessage for Synapse. It
// first sends as the admin identity; when the admin is not joined to the room
// (events_default=0, but the sender must still be in-room, event_auth.py:731)
// or lacks power, it force-joins the admin (or make_room_admin) and retries.
func (o *SynapseMatrixOps) SendSystemMessage(ctx context.Context, roomID, body string) error {
	err := o.sendMessageAsAdmin(ctx, roomID, body)
	if err == nil {
		return nil
	}
	return o.recoverSynapseSenderOp(ctx, roomID, "send system message to", o.UserID(o.config.AdminUser), func() error {
		return o.sendMessageAsAdmin(ctx, roomID, body)
	}, err)
}

// ListRoomMembers implements MatrixOps.ListRoomMembers for Synapse. Reads via
// the CS API; the admin identity bypasses in-room checks
// (auth/base.py:206), so no fallback is needed. When member.ActorToken is set
// the read uses that token instead.
func (o *SynapseMatrixOps) ListRoomMembers(ctx context.Context, roomID string, member MemberSpec) ([]RoomMember, error) {
	return listRoomMembersForMember(ctx, o.matrixClient, roomID, member)
}

// ListJoinedRooms implements MatrixOps.ListJoinedRooms for Synapse, listing
// the rooms the user described by member is joined to (empty ActorToken →
// admin identity).
func (o *SynapseMatrixOps) ListJoinedRooms(ctx context.Context, member MemberSpec) ([]string, error) {
	token, err := memberTokenFor(ctx, member, o.ensureAdminToken)
	if err != nil {
		return nil, fmt.Errorf("list joined rooms: %w", err)
	}
	return o.matrixClient.ListJoinedRooms(ctx, token)
}

// IsUserInRoom implements MatrixOps.IsUserInRoom for Synapse. Pure read via
// the admin identity (bypasses in-room checks).
func (o *SynapseMatrixOps) IsUserInRoom(ctx context.Context, roomID, userID string) (bool, error) {
	return isUserInRoomForMember(ctx, o.matrixClient, roomID, userID)
}

// IsManagerJoinedDM implements MatrixOps.IsManagerJoinedDM for Synapse. Pure
// read via the admin identity; safe to poll on every reconcile.
func (o *SynapseMatrixOps) IsManagerJoinedDM(ctx context.Context, roomID string) (bool, error) {
	return isManagerJoinedDMForMember(ctx, o.matrixClient, roomID)
}

// HealthCheck implements MatrixOps.HealthCheck for Synapse by attempting a
// login with deliberately-invalid credentials. Any HTTP-level response (401,
// 403, ...) means the server is up; a transport error (connection refused,
// DNS failure, EOF) is returned.
func (o *SynapseMatrixOps) HealthCheck(ctx context.Context) error {
	_, err := o.matrixClient.Login(ctx, "__healthcheck__", "invalid")
	if err != nil && isMatrixConnError(err) {
		return err
	}
	return nil
}

// === Phase 4: User Identity & Credentials + AppService Governance ===

// ProvisionUser implements MatrixOps.ProvisionUser for Synapse via the admin
// REST API (PUT /_synapse/admin/v2/users/{id} creates the user with a
// password, then a CS login returns the access token). Idempotent: PUT sets
// the password whether the user is new or already exists. An empty
// spec.Password yields a generated one, returned in the UserCredentials.
// NOTE: SynapseClient.EnsureUser always reports Created=true (the admin API
// has no register-vs-login distinction), so UserRef.Created is not meaningful
// on Synapse.
func (o *SynapseMatrixOps) ProvisionUser(ctx context.Context, spec UserSpec) (*UserRef, *UserCredentials, error) {
	uc, err := o.synapseAdmin.EnsureUser(ctx, EnsureUserRequest{Username: spec.Username, Password: spec.Password})
	if err != nil {
		return nil, nil, err
	}
	return &UserRef{UserID: uc.UserID, Created: uc.Created}, uc, nil
}

// ProvisionUserViaAppService implements MatrixOps.ProvisionUserViaAppService
// for Synapse via the CS register endpoint authenticated with the as_token
// (m.login.application_service) — the standard Matrix AppService user
// provisioning flow, which Synapse supports identically to Tuwunel. Requires
// the AppService to be registered (declaratively, via Helm).
func (o *SynapseMatrixOps) ProvisionUserViaAppService(ctx context.Context, localpart string) (*UserRef, *UserCredentials, error) {
	uc, err := o.matrixClient.EnsureAppServiceUser(ctx, localpart)
	if err != nil {
		return nil, nil, err
	}
	return &UserRef{UserID: uc.UserID, Created: uc.Created}, uc, nil
}

// LoginUser implements MatrixOps.LoginUser for Synapse via password login.
func (o *SynapseMatrixOps) LoginUser(ctx context.Context, username, password string) (string, error) {
	return o.matrixClient.Login(ctx, username, password)
}

// LoginUserViaAppService implements MatrixOps.LoginUserViaAppService for
// Synapse via the AS login flow (m.login.application_service).
func (o *SynapseMatrixOps) LoginUserViaAppService(ctx context.Context, localpart string) (string, error) {
	return o.matrixClient.LoginAppServiceUser(ctx, localpart)
}

// ResetUserPassword implements MatrixOps.ResetUserPassword for Synapse via
// POST /_synapse/admin/v1/reset_password/{userID}.
func (o *SynapseMatrixOps) ResetUserPassword(ctx context.Context, userID, password string) error {
	return o.synapseAdmin.synResetPassword(ctx, userID, password)
}

// DeactivateUser implements MatrixOps.DeactivateUser for Synapse via
// POST /_synapse/admin/v1/deactivate/{userID} with erase=false (data is
// preserved, matching Tuwunel's deactivate semantics).
func (o *SynapseMatrixOps) DeactivateUser(ctx context.Context, userID string) error {
	return o.synapseAdmin.synDeactivateUser(ctx, userID)
}

// SetUserDisplayName implements MatrixOps.SetUserDisplayName for Synapse. With
// a user access token it uses the CS profile endpoint; with an empty token it
// falls back to the admin REST users endpoint (which can set any user's
// displayname without touching the password).
func (o *SynapseMatrixOps) SetUserDisplayName(ctx context.Context, userID, accessToken, displayName string) error {
	if accessToken == "" {
		return o.synapseAdmin.synSetDisplayName(ctx, userID, displayName)
	}
	return o.matrixClient.SetDisplayName(ctx, userID, accessToken, displayName)
}

// VerifyUserAccessToken implements MatrixOps.VerifyUserAccessToken for Synapse
// via GET /_matrix/client/v3/account/whoami.
func (o *SynapseMatrixOps) VerifyUserAccessToken(ctx context.Context, accessToken string) error {
	return o.matrixClient.VerifyAccessToken(ctx, accessToken)
}

// UserIDFor implements MatrixOps.UserIDFor for Synapse: pure formatting of
// "@<localpart>:<domain>".
func (o *SynapseMatrixOps) UserIDFor(localpart string) string {
	return o.matrixClient.UserID(localpart)
}

// BackfillLegacyPassword implements MatrixOps.BackfillLegacyPassword for
// Synapse. Same underlying admin operation as ResetUserPassword (admin REST
// reset_password), kept separate to signal the bulk-migration intent.
func (o *SynapseMatrixOps) BackfillLegacyPassword(ctx context.Context, userID, password string) error {
	return o.synapseAdmin.synResetPassword(ctx, userID, password)
}

// RegisterAppService implements MatrixOps.RegisterAppService for Synapse.
// Registrations are declarative (Helm-managed via the app_service_config_files
// mechanism) — there is no runtime registration API. The implementation only
// verifies the existing registration is active via a smoke test and reports an
// error (pointing at the Helm config) when it is not.
func (o *SynapseMatrixOps) RegisterAppService(ctx context.Context, reg AppServiceRegistration) error {
	if err := o.matrixClient.AppServiceSmokeTest(ctx); err != nil {
		return fmt.Errorf("synapse appservice %q is not active: registrations are declarative (Helm-managed); "+
			"update the chart's matrix.appservice.* values and the homeserver app_service_config_files, then re-apply: %w",
			reg.ID, err)
	}
	return nil
}

// UnregisterAppService implements MatrixOps.UnregisterAppService for Synapse.
// Registrations are declarative (Helm-managed) and cannot be removed at
// runtime; the returned error points the operator at the Helm chart.
func (o *SynapseMatrixOps) UnregisterAppService(ctx context.Context, id string) error {
	return fmt.Errorf("synapse appservice %q cannot be unregistered at runtime: registrations are declarative "+
		"(Helm-managed); remove it from the chart's matrix.appservice values and re-apply", id)
}

// SmokeTestAppService implements MatrixOps.SmokeTestAppService for Synapse via
// an AS login as the sender_localpart user.
func (o *SynapseMatrixOps) SmokeTestAppService(ctx context.Context) error {
	return o.matrixClient.AppServiceSmokeTest(ctx)
}
