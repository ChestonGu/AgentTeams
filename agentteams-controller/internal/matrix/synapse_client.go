package matrix

import (
	"context"
	"fmt"
	"net/http"
	"net/url"
)

// SynapseClient implements Client for Synapse homeservers.
//
// It reuses TuwunelClient's Matrix client-server API methods verbatim —
// both homeservers implement the same /_matrix/client/v3/* surface, so the
// standard CS methods (Login, CreateRoom, JoinRoom, SendMessage, …) need no
// change. EnsureUser is overridden so user provisioning goes through the
// Synapse admin REST API (PUT /_synapse/admin/v2/users/{id}) instead of
// Tuwunel's registration_token flow, which Synapse 1.127 rejects as a
// single-step UI auth submission.
//
// Admin operations that Tuwunel drives through its "!admin ..." chat bot
// (AdminCommand, SetPasswordAsAdmin, runtime AppService register/unregister)
// are NOT supported here: Synapse has no equivalent chat bot. Those operations
// live on the concrete *TuwunelClient type and are surfaced at the business
// layer through SynapseMatrixOps (which calls the Synapse admin REST helpers
// below directly). AdminCommand is explicitly overridden to return an error
// so the inherited TuwunelClient.AdminCommand (which would silently send a
// "!admin" message into a Synapse room) can never leak through the embedded
// pointer.
type SynapseClient struct {
	*TuwunelClient
}

// Compile-time assertion that SynapseClient satisfies the Client interface.
var _ Client = (*SynapseClient)(nil)

// NewSynapseClient creates a Matrix client for a Synapse homeserver.
func NewSynapseClient(cfg Config, httpClient *http.Client) *SynapseClient {
	return &SynapseClient{TuwunelClient: NewTuwunelClient(cfg, httpClient)}
}

// AdminCommand is a Tuwunel-only concept (sends a "!admin ..." chat message to
// the Tuwunel admin bot room). Synapse has no admin bot — it exposes a REST
// admin API consumed directly by the SynapseMatrixOps layer. Override the
// inherited TuwunelClient.AdminCommand with an explicit error so a stray call
// through the embedded pointer cannot silently deliver a "!admin" message into
// a Synapse room. Callers that need a Synapse admin operation should use the
// SynapseMatrixOps methods (DissolveRoom, DeactivateUser, ResetUserPassword,
// …), which call synAdminCall / MakeRoomAdmin / etc. directly.
func (s *SynapseClient) AdminCommand(ctx context.Context, command string) error {
	return fmt.Errorf("synapse: AdminCommand %q not supported — Synapse has no admin bot; use SynapseMatrixOps methods (REST admin API) instead", command)
}

// synAdminCall issues a Synapse admin REST request with the cached admin
// token and treats 2xx as success. The admin account (AGENTTEAMS_ADMIN_USER)
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

// synDeactivateUser deactivates a user account via the Synapse admin API.
// erase=false keeps the user's data (room memberships, messages) intact —
// matching Tuwunel's deactivate semantics, which also do not erase.
func (s *SynapseClient) synDeactivateUser(ctx context.Context, userID string) error {
	path := "/_synapse/admin/v1/deactivate/" + url.PathEscape(userID)
	return s.synAdminCall(ctx, http.MethodPost, path, map[string]bool{"erase": false})
}

// synSetDisplayName updates a user's displayname via the Synapse admin REST
// users endpoint (PUT /_synapse/admin/v2/users/{id} accepts a displayname
// field without touching the password). Used as the admin-identity fallback
// when no user access token is available.
func (s *SynapseClient) synSetDisplayName(ctx context.Context, userID, displayName string) error {
	path := "/_synapse/admin/v2/users/" + url.PathEscape(userID)
	return s.synAdminCall(ctx, http.MethodPut, path, map[string]string{"displayname": displayName})
}

// EnsureUser creates (or re-creates) the Matrix user via the Synapse admin
// API, then logs in to obtain an access token. This is Synapse-specific:
// Tuwunel's client /register with m.login.registration_token is single-step,
// but Synapse's registration_token UI auth requires a session (two-step) and
// rejects the single-step submission with M_INVALID_PARAM "Invalid login
// submission". The admin API (PUT /_synapse/admin/v2/users/{id}) creates the
// user directly with a password — no registration_token / session needed.
// Idempotent: PUT sets the password whether the user is new or already exists.
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

	// Create or update the user via the Synapse admin API.
	path := "/_synapse/admin/v2/users/" + url.PathEscape(userID)
	body := map[string]interface{}{
		"password":    password,
		"displayname": req.Username,
	}
	if err := s.synAdminCall(ctx, http.MethodPut, path, body); err != nil {
		return nil, fmt.Errorf("synapse create user %s: %w", req.Username, err)
	}

	// Login to obtain an access token for the (now guaranteed) account.
	token, err := s.Login(ctx, req.Username, password)
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
