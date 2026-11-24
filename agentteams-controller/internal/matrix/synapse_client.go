package matrix

import (
	"context"
	"fmt"
	"net/http"
	"net/url"

	"sigs.k8s.io/controller-runtime/pkg/log"
)

// SynapseClient implements Client for Synapse homeservers (verified against
// Synapse v1.127).
//
// It embeds the provider-neutral *matrixClient, which carries every standard
// Matrix CS API operation (/createRoom, /invite, /kick, /state, /send, /sync,
// login, AppService user login/registration), and overrides the operations
// where Synapse differs from Tuwunel:
//
//   - EnsureUser: Synapse has no single-step registration_token flow. Users are
//     created/updated through the admin REST API
//     (PUT /_synapse/admin/v2/users/{id}), then logged in via the CS API.
//   - SetPasswordAsAdmin / ResetUserPassword: admin REST
//     POST /_synapse/admin/v1/reset_password/{id}.
//   - AdminCommand: Synapse has no admin bot; the method returns an explicit
//     error. Provider-native admin operations are exposed through
//     ProviderAdminOps (DeleteRoom / ForceLeaveRoom / DeactivateUser).
//   - RegisterAppService / UnregisterAppService: Synapse AppService
//     registrations are declarative (app_service_config_files mounted as a
//     file). Register performs a smoke-test verification only; Unregister
//     fails with an operator hint.
//   - In-room CS operations (invite/kick/state/system messages): a failed
//     sender-side check (not joined / insufficient power) is recovered with
//     Synapse's own admin capabilities — POST /_synapse/admin/v1/join/{roomID}
//     to restore membership and
//     POST /_synapse/admin/v1/rooms/{roomID}/make_room_admin to raise the
//     sender's power level — then the CS operation is retried. Synapse 1.127
//     has no admin kick endpoint; kicks always go through the CS API.
//
// The admin account (AGENTTEAMS_ADMIN_USER) must be a Synapse server admin
// (created by the bootstrap step with register_new_matrix_user -a), otherwise
// the /_synapse/admin/* endpoints answer 403.
type SynapseClient struct {
	*matrixClient
}

// Compile-time assertions.
var (
	_ Client           = (*SynapseClient)(nil)
	_ ProviderAdminOps = (*SynapseClient)(nil)
)

// NewSynapseClient creates a Matrix client for a Synapse homeserver.
func NewSynapseClient(cfg Config, httpClient *http.Client) *SynapseClient {
	return &SynapseClient{matrixClient: newMatrixClient(cfg, httpClient)}
}

// EnsureUser creates (or re-creates) the Matrix user through the Synapse admin
// REST API, then logs in to obtain an access token.
//
// Synapse's registration_token UI auth requires a two-step session and rejects
// the single-step submission used by Tuwunel ("Invalid login submission"), so
// the account is created with PUT /_synapse/admin/v2/users/{id} instead — no
// registration token or session is needed. The PUT is idempotent and keeps
// existing device sessions (logout_devices=false) so idempotent re-provisions
// do not kill running worker/human sessions. deactivated=false reactivates a
// leftover account, because Synapse cannot hard-delete users and a deactivated
// row still occupies the user ID.
func (s *SynapseClient) EnsureUser(ctx context.Context, req EnsureUserRequest) (*UserCredentials, error) {
	password := req.Password
	if password == "" {
		var err error
		password, err = GeneratePassword(16)
		if err != nil {
			return nil, fmt.Errorf("generate password: %w", err)
		}
	}
	userID := s.UserID(req.Username)

	path := "/_synapse/admin/v2/users/" + url.PathEscape(userID)
	body := map[string]interface{}{
		"password":       password,
		"logout_devices": false,
		"deactivated":    false,
	}
	if err := s.synAdminCall(ctx, http.MethodPut, path, body, nil); err != nil {
		return nil, fmt.Errorf("synapse create user %s: %w", req.Username, err)
	}

	// loginFresh bypasses the cached-token fast path: a token cached before the
	// password (re)apply must not short-circuit the post-create login.
	token, err := s.loginFresh(ctx, req.Username, password)
	if err != nil {
		return nil, fmt.Errorf("synapse login %s after create: %w", req.Username, err)
	}
	return &UserCredentials{
		UserID:      userID,
		AccessToken: token,
		Password:    password,
		Created:     true,
	}, nil
}

// AdminCommand is a Tuwunel-only concept (a "!admin ..." chat message to the
// Tuwunel admin bot room). Synapse has no admin bot — the controller talks to
// the Synapse admin REST API directly. This method returns an explicit error as
// a regression guard so a Synapse deployment can never silently post a "!admin"
// message into a room; business callers that need privileged operations must go
// through ProviderAdminOps instead.
func (s *SynapseClient) AdminCommand(ctx context.Context, command string) error {
	return fmt.Errorf("synapse: AdminCommand %q not supported — Synapse has no admin bot; use ProviderAdminOps (DeleteRoom/ForceLeaveRoom/DeactivateUser) or the Synapse admin REST API instead", command)
}

// SetPasswordAsAdmin sets a user's password through the Synapse admin API.
// logout_devices=false mirrors the Tuwunel "--no-logout-devices" semantics used
// for the initial Human password so a freshly issued AppService token survives.
func (s *SynapseClient) SetPasswordAsAdmin(ctx context.Context, userID, password string) error {
	if err := s.synResetPassword(ctx, userID, password, false); err != nil {
		return err
	}
	s.InvalidateUserToken(userID)
	return nil
}

// ResetUserPassword changes an existing password through the Synapse admin API,
// logging the account's devices out (the user explicitly requested a new
// password; any existing session token becomes suspect).
func (s *SynapseClient) ResetUserPassword(ctx context.Context, userID, password string) error {
	if err := s.synResetPassword(ctx, userID, password, true); err != nil {
		return err
	}
	s.InvalidateUserToken(userID)
	return nil
}

// CreateRoom creates a room through the CS API, applying Synapse's stricter
// createRoom validation first:
//
//   - Synapse rejects a power_level_content_override that omits the room
//     creator (HTTP 400 M_BAD_JSON), so the creating identity is pinned to
//     power level 100 when a power-level map was supplied.
//   - Synapse rejects createRoom with 403 when the invite list contains the
//     creating user — after the room and alias already exist, silently dropping
//     the remaining invites. The creator is stripped from the invite list.
func (s *SynapseClient) CreateRoom(ctx context.Context, req CreateRoomRequest) (*RoomInfo, error) {
	creator := ""
	if req.CreatorToken == "" {
		creator = s.UserID(s.config.AdminUser)
	} else if userID, err := s.accessTokenUserID(ctx, req.CreatorToken); err == nil {
		creator = userID
	}

	if creator != "" {
		if len(req.PowerLevels) > 0 {
			if _, ok := req.PowerLevels[creator]; !ok {
				levels := make(map[string]int, len(req.PowerLevels)+1)
				for user, level := range req.PowerLevels {
					levels[user] = level
				}
				levels[creator] = 100
				req.PowerLevels = levels
			}
		}
		req.Invite = withoutUser(req.Invite, creator)
	}
	return s.matrixClient.CreateRoom(ctx, req)
}

// withoutUser returns invite without userID's entry (order preserved).
func withoutUser(invite []string, userID string) []string {
	if len(invite) == 0 || userID == "" {
		return invite
	}
	out := make([]string, 0, len(invite))
	for _, u := range invite {
		if u != userID {
			out = append(out, u)
		}
	}
	return out
}

// InviteToRoom invites userID using the admin identity and recovers from a
// sender-side rejection (admin not joined / lacks power) via Synapse's admin
// APIs before retrying.
func (s *SynapseClient) InviteToRoom(ctx context.Context, roomID, userID string) error {
	err := s.matrixClient.InviteToRoom(ctx, roomID, userID)
	if err == nil {
		return nil
	}
	return s.recoverSenderOp(ctx, roomID, "invite "+userID+" to", s.UserID(s.config.AdminUser), func() error {
		return s.matrixClient.InviteToRoom(ctx, roomID, userID)
	}, err)
}

// KickFromRoom kicks userID using the admin identity with the same recovery
// strategy as InviteToRoom. When the failure is an equal-power rejection (a
// server admin at PL 100 cannot kick another PL-100 user), the target's power
// level is lowered first and the kick retried.
func (s *SynapseClient) KickFromRoom(ctx context.Context, roomID, userID, reason string) error {
	err := s.matrixClient.KickFromRoom(ctx, roomID, userID, reason)
	if err == nil {
		return nil
	}
	recoveryErr := s.recoverSenderOp(ctx, roomID, "kick "+userID+" from", s.UserID(s.config.AdminUser), func() error {
		return s.matrixClient.KickFromRoom(ctx, roomID, userID, reason)
	}, err)
	if recoveryErr == nil {
		return nil
	}
	// Final attempt for equal-power targets: demote the target below the
	// kicker's power level, kick, then restore on failure (see
	// synDemoteAndKick). This is what makes "manager is PL 100 in a worker's
	// personal room, controller admin is also PL 100" converge.
	if demoteErr := s.synDemoteAndKick(ctx, roomID, userID, func() error {
		return s.matrixClient.KickFromRoom(ctx, roomID, userID, reason)
	}); demoteErr == nil {
		return nil
	}
	return recoveryErr
}

// SetRoomName renames a room and recovers from a sender-side rejection when the
// rename is performed by the admin identity (explicit actor tokens are left to
// the caller: the recovery would otherwise change who is acting).
func (s *SynapseClient) SetRoomName(ctx context.Context, roomID, name, userToken string) error {
	err := s.matrixClient.SetRoomName(ctx, roomID, name, userToken)
	if err == nil || userToken != "" {
		return err
	}
	return s.recoverSenderOp(ctx, roomID, "rename", s.UserID(s.config.AdminUser), func() error {
		return s.matrixClient.SetRoomName(ctx, roomID, name, "")
	}, err)
}

// SetRoomState writes a state event and recovers from a sender-side rejection
// when the write is performed by the admin identity.
func (s *SynapseClient) SetRoomState(ctx context.Context, roomID, eventType, stateKey string, content map[string]interface{}, userToken string) error {
	err := s.matrixClient.SetRoomState(ctx, roomID, eventType, stateKey, content, userToken)
	if err == nil || userToken != "" {
		return err
	}
	return s.recoverSenderOp(ctx, roomID, "set state "+eventType, s.UserID(s.config.AdminUser), func() error {
		return s.matrixClient.SetRoomState(ctx, roomID, eventType, stateKey, content, "")
	}, err)
}

// SendMessageAsAdmin sends a plain-text message using the admin identity and
// recovers from a sender-side rejection (admin not joined / lacks power).
func (s *SynapseClient) SendMessageAsAdmin(ctx context.Context, roomID, body string) error {
	err := s.matrixClient.SendMessageAsAdmin(ctx, roomID, body)
	if err == nil {
		return nil
	}
	return s.recoverSenderOp(ctx, roomID, "send admin message to", s.UserID(s.config.AdminUser), func() error {
		return s.matrixClient.SendMessageAsAdmin(ctx, roomID, body)
	}, err)
}

// SendNotification sends a mention notification using the admin identity and
// recovers from a sender-side rejection.
func (s *SynapseClient) SendNotification(ctx context.Context, roomID, body string, mentionUserIDs []string) error {
	err := s.matrixClient.SendNotification(ctx, roomID, body, mentionUserIDs)
	if err == nil {
		return nil
	}
	return s.recoverSenderOp(ctx, roomID, "send notification to", s.UserID(s.config.AdminUser), func() error {
		return s.matrixClient.SendNotification(ctx, roomID, body, mentionUserIDs)
	}, err)
}

// RegisterAppService verifies that the declarative AppService registration is
// already active. Synapse appservices are registered by the homeserver at
// startup from app_service_config_files; there is no runtime registration API.
func (s *SynapseClient) RegisterAppService(ctx context.Context, reg AppServiceRegistration) error {
	if err := s.AppServiceSmokeTest(ctx); err != nil {
		return fmt.Errorf("synapse appservice %q is not active: registrations are declarative (app_service_config_files); "+
			"update the mounted registration Secret / chart values, then restart Synapse: %w", reg.ID, err)
	}
	return nil
}

// UnregisterAppService is not supported on Synapse: registrations are
// declarative and can only be removed by editing the registration file and
// restarting the homeserver.
func (s *SynapseClient) UnregisterAppService(ctx context.Context, id string) error {
	return fmt.Errorf("synapse appservice %q cannot be unregistered at runtime: registrations are declarative "+
		"(app_service_config_files); remove it from the mounted registration Secret and restart Synapse", id)
}

// DeleteRoom shuts down and purges roomID via the Synapse admin API
// (DELETE /_synapse/admin/v2/rooms/{roomID}). The purge runs asynchronously
// inside Synapse; this call is fire-and-forget from the controller's point of
// view (matching the "!admin rooms delete-room" semantics on Tuwunel).
func (s *SynapseClient) DeleteRoom(ctx context.Context, roomID string) error {
	if roomID == "" {
		return nil
	}
	path := "/_synapse/admin/v2/rooms/" + url.PathEscape(roomID)
	// Synapse 1.127 parses the DELETE body via parse_json_object_from_request
	// without allow_empty_body, so an empty body returns 400 M_NOT_JSON "Content
	// not JSON."; `{}` satisfies the parser (block/purge default false/true).
	return s.synAdminCall(ctx, http.MethodDelete, path, map[string]interface{}{}, nil)
}

// ForceLeaveRoom removes userID from roomID without requiring the user's own
// access token (the Human delete flow may no longer hold a valid user token).
// Synapse has no force-leave admin endpoint, so this restores the admin's
// ability to kick — join (POST /_synapse/admin/v1/join) and
// make_room_admin (POST /_synapse/admin/v1/rooms/{id}/make_room_admin) as
// needed — and then kicks through the CS API. Idempotent: a user who is not in
// the room returns nil.
func (s *SynapseClient) ForceLeaveRoom(ctx context.Context, userID, roomID string) error {
	if userID == "" || roomID == "" {
		return nil
	}
	kick := func() error {
		return s.matrixClient.KickFromRoom(ctx, roomID, userID, "force-leave (admin)")
	}
	err := kick()
	if err == nil {
		return nil
	}
	recoveryErr := s.recoverSenderOp(ctx, roomID, "force-leave "+userID+" from", s.UserID(s.config.AdminUser), kick, err)
	if recoveryErr == nil {
		return nil
	}
	if demoteErr := s.synDemoteAndKick(ctx, roomID, userID, kick); demoteErr == nil {
		return nil
	}
	return recoveryErr
}

// DeactivateUser disables a Matrix account through the Synapse admin API
// (POST /_synapse/admin/v1/deactivate/{userID} with erase=false, preserving
// room memberships and message history — matching Tuwunel's deactivate
// semantics). The Synapse row is kept (orders are not hard-deletable), so a
// later re-provisioning of the same localpart reactivates it via EnsureUser.
func (s *SynapseClient) DeactivateUser(ctx context.Context, userID string) error {
	if userID == "" {
		return nil
	}
	path := "/_synapse/admin/v1/deactivate/" + url.PathEscape(userID)
	if err := s.synAdminCall(ctx, http.MethodPost, path, map[string]bool{"erase": false}, nil); err != nil {
		return fmt.Errorf("synapse deactivate %s: %w", userID, err)
	}
	s.InvalidateUserToken(userID)
	return nil
}

// logSynapseRecovery records which Synapse admin recovery was applied to a
// failed in-room CS operation. Best-effort observability only.
func (s *SynapseClient) logSynapseRecovery(ctx context.Context, roomID, op string, recovery synapseSenderRecovery, err error) {
	log.FromContext(ctx).Info("synapse: recovering rejected in-room operation",
		"room", roomID, "operation", op, "recovery", recovery.String(), "error", err.Error())
}
