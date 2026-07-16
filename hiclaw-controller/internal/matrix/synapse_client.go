package matrix

import (
	"context"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// SynapseClient implements Client for Synapse homeservers.
//
// It reuses TuwunelClient's Matrix client-server API methods verbatim —
// both homeservers implement the same /_matrix/client/v3/* surface, so the
// 17 standard methods (Login, CreateRoom, JoinRoom, SendMessage, …) need no
// change. The ONLY divergence is administration: Tuwunel drives admin ops by
// sending "!admin ..." chat messages to a built-in admin bot room, while
// Synapse exposes a REST admin API (/_synapse/admin/v1/*). SynapseClient
// therefore overrides AdminCommand to parse the legacy "!admin ..." command
// strings emitted by the service layer and translate them to Synapse REST.
//
// EnsureUser is also overridden (copied verbatim from TuwunelClient) so its
// orphan-recovery AdminCommand call dispatches to this Synapse translation
// instead of the embedded TuwunelClient's chat-based one (Go does not give
// virtual dispatch through an embedded type's methods, so the copy is what
// makes c.AdminCommand resolve to the override below).
//
// Note: v1.1.2's controller is NOT a Matrix AppService (no as_token /
// /_matrix/app/* code), so there is no runtime AppService-registration path
// to translate — the homeserver is configured declaratively in both cases.
type SynapseClient struct {
	*TuwunelClient
}

// Compile-time assertion that SynapseClient satisfies the Client interface.
var _ Client = (*SynapseClient)(nil)

// NewSynapseClient creates a Matrix client for a Synapse homeserver.
func NewSynapseClient(cfg Config, httpClient *http.Client) *SynapseClient {
	return &SynapseClient{TuwunelClient: NewTuwunelClient(cfg, httpClient)}
}

// AdminCommand translates a legacy "!admin ..." command to Synapse admin REST.
// These are the only forms v1.1.2's service layer emits:
//
//	!admin users reset-password   <userID> <password>  -> POST /_synapse/admin/v1/reset_password/{userID}
//	!admin users force-leave-room <userID> <roomID>    -> POST /_synapse/admin/v1/rooms/{roomID}/kick
//	!admin rooms  delete-room     <roomID>             -> DELETE /_synapse/admin/v2/rooms/{roomID}
//
// Any other "!admin" form returns an error (surfaced in controller logs).
func (s *SynapseClient) AdminCommand(ctx context.Context, command string) error {
	command = strings.TrimSpace(command)
	f := strings.Fields(command)
	if len(f) < 2 || f[0] != "!admin" {
		return fmt.Errorf("synapse admin: not an !admin command: %q", command)
	}
	switch {
	case len(f) >= 5 && f[1] == "users" && f[2] == "reset-password":
		return s.synResetPassword(ctx, f[3], f[4])
	case len(f) >= 5 && f[1] == "users" && f[2] == "force-leave-room":
		return s.synKick(ctx, f[4], f[3]) // roomID, userID
	case len(f) >= 4 && f[1] == "rooms" && f[2] == "delete-room":
		return s.synDeleteRoom(ctx, f[3])
	default:
		return fmt.Errorf("synapse admin: unsupported !admin command: %q", command)
	}
}

// synAdminCall issues a Synapse admin REST request with the cached admin
// token and treats 2xx as success. The admin account (HICLAW_ADMIN_USER)
// must be a Synapse server admin, or these endpoints return 403.
func (s *SynapseClient) synAdminCall(ctx context.Context, method, path string, body interface{}) error {
	token, err := s.ensureAdminToken(ctx)
	if err != nil {
		return fmt.Errorf("synapse admin %s %s: %w", method, path, err)
	}
	statusCode, respBody, err := s.doJSON(ctx, method, path, token, body, nil)
	if err != nil {
		return fmt.Errorf("synapse admin %s %s: %w", method, path, err)
	}
	if statusCode < 200 || statusCode >= 300 {
		return fmt.Errorf("synapse admin %s %s: HTTP %d: %s", method, path, statusCode, truncate(respBody, 500))
	}
	return nil
}

func (s *SynapseClient) synResetPassword(ctx context.Context, userID, password string) error {
	path := "/_synapse/admin/v1/reset_password/" + url.PathEscape(userID)
	return s.synAdminCall(ctx, http.MethodPost, path, map[string]string{"new_password": password})
}

func (s *SynapseClient) synKick(ctx context.Context, roomID, userID string) error {
	path := "/_synapse/admin/v1/rooms/" + url.PathEscape(roomID) + "/kick"
	return s.synAdminCall(ctx, http.MethodPost, path, map[string]string{"user_id": userID})
}

func (s *SynapseClient) synDeleteRoom(ctx context.Context, roomID string) error {
	path := "/_synapse/admin/v2/rooms/" + url.PathEscape(roomID)
	return s.synAdminCall(ctx, http.MethodDelete, path, nil)
}

// EnsureUser is a verbatim copy of TuwunelClient.EnsureUser, redefined on
// SynapseClient so the orphan-recovery AdminCommand call dispatches to the
// Synapse translation above. Keep in sync with TuwunelClient.EnsureUser.
func (s *SynapseClient) EnsureUser(ctx context.Context, req EnsureUserRequest) (*UserCredentials, error) {
	password := req.Password
	if password == "" {
		var err error
		password, err = GeneratePassword(16)
		if err != nil {
			return nil, fmt.Errorf("generate password: %w", err)
		}
	}

	// Try registration first
	regBody := map[string]interface{}{
		"username": req.Username,
		"password": password,
		"auth": map[string]string{
			"type":  "m.login.registration_token",
			"token": s.config.RegistrationToken,
		},
	}
	var regResp struct {
		UserID      string `json:"user_id"`
		AccessToken string `json:"access_token"`
		ErrCode     string `json:"errcode"`
		Error       string `json:"error"`
	}

	statusCode, _, err := s.doJSON(ctx, http.MethodPost,
		"/_matrix/client/v3/register", "", regBody, &regResp)
	if err != nil {
		return nil, fmt.Errorf("register user %s: %w", req.Username, err)
	}

	if statusCode == http.StatusOK || statusCode == http.StatusCreated {
		return &UserCredentials{
			UserID:      regResp.UserID,
			AccessToken: regResp.AccessToken,
			Password:    password,
			Created:     true,
		}, nil
	}

	// Only fall back to login if the user already exists
	if regResp.ErrCode != "" && regResp.ErrCode != "M_USER_IN_USE" {
		return nil, fmt.Errorf("register user %s: %s (%s)", req.Username, regResp.ErrCode, regResp.Error)
	}

	// Registration failed with M_USER_IN_USE — try login
	token, err := s.Login(ctx, req.Username, password)
	if err == nil {
		return &UserCredentials{
			UserID:      s.UserID(req.Username),
			AccessToken: token,
			Password:    password,
			Created:     false,
		}, nil
	}

	// Orphan recovery: login fails because the account exists with a different
	// password. Reset it via Synapse admin REST (translated AdminCommand) and
	// retry login.
	userID := s.UserID(req.Username)
	cmd := fmt.Sprintf("!admin users reset-password %s %s", userID, password)
	if adminErr := s.AdminCommand(ctx, cmd); adminErr != nil {
		return nil, fmt.Errorf("user %s exists but login failed (%v) and orphan recovery failed: %w",
			req.Username, err, adminErr)
	}

	const maxAttempts = 5
	baseDelay := s.orphanRetryBaseDelay
	if baseDelay <= 0 {
		baseDelay = 500 * time.Millisecond
	}
	var lastErr = err
	for attempt := 1; attempt <= maxAttempts; attempt++ {
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(baseDelay * time.Duration(attempt)):
		}
		token, lastErr = s.Login(ctx, req.Username, password)
		if lastErr == nil {
			return &UserCredentials{
				UserID:      userID,
				AccessToken: token,
				Password:    password,
				Created:     false,
			}, nil
		}
	}
	return nil, fmt.Errorf("user %s exists, orphan recovery issued but login still failing: %w",
		req.Username, lastErr)
}
