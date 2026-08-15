package service

import (
	"context"
	"fmt"

	"github.com/agentscope-ai/AgentTeams/agentteams-controller/internal/matrix"
	"sigs.k8s.io/controller-runtime/pkg/log"
)

// =========================================================================
// Decomposed primitives — five explicit one-action calls
// =========================================================================
//
// The original EnsureHumanUser / LoginAsHuman composites bundled
// "register + set password" and "AS-or-password login" into single
// black boxes. That coupling made it impossible to express
// per-identity-type behaviour (a SSO Human must register without ever
// being assigned a password, for example) without growing if/else
// branches inside the composite. The decomposition below splits each
// composite into the smallest semantic unit so callers — both legacy
// reconcile paths and future identity-source implementations — can
// pick exactly the steps they need.
//
// All five methods are pure adapters over internal/matrix; the
// decision about *whether* to invoke a given step lives at the call
// site.

// RegisterAppServiceUser performs a single AS-register call. When the
// account already exists (M_USER_IN_USE) the underlying client falls
// back to LoginAppServiceUser and reports Created=false. The returned
// HumanCredentials carries an empty Password — the AS protocol does
// not assign one and callers that want password login must follow up
// with SetUserPassword explicitly.
func (p *Provisioner) RegisterAppServiceUser(ctx context.Context, username string) (*HumanCredentials, error) {
	_, uc, err := p.matrixOps.ProvisionUserViaAppService(ctx, username)
	if err != nil {
		return nil, fmt.Errorf("AS register human %s: %w", username, err)
	}
	return &HumanCredentials{
		UserID:      uc.UserID,
		AccessToken: uc.AccessToken,
		Password:    "",
		Created:     uc.Created,
	}, nil
}

// RegisterLegacyUser provisions a password-mode Matrix account via
// MatrixOps.ProvisionUser. On existing accounts the provider falls through
// to password reset + login. The returned HumanCredentials always carries a
// Password since legacy auth has no AS bypass.
func (p *Provisioner) RegisterLegacyUser(ctx context.Context, username string) (*HumanCredentials, error) {
	_, uc, err := p.matrixOps.ProvisionUser(ctx, matrix.UserSpec{Username: username})
	if err != nil {
		return nil, fmt.Errorf("register legacy human %s: %w", username, err)
	}
	return &HumanCredentials{
		UserID:      uc.UserID,
		AccessToken: uc.AccessToken,
		Password:    uc.Password,
		Created:     uc.Created,
	}, nil
}

// SetUserPassword writes a password for an existing Matrix account via
// the admin bot. Best-effort — admin command delivery is confirmed but
// the bot itself executes the reset asynchronously. Callers that must
// confirm propagation are expected to test by attempting a login
// afterwards.
func (p *Provisioner) SetUserPassword(ctx context.Context, userID, password string) error {
	return p.matrixOps.ResetUserPassword(ctx, userID, password)
}

// LoginAppServiceUser obtains a fresh access token via the AS login
// flow (no password required). Used by both legacy_password and
// external_sso identity sources when the controller runs in AS mode.
func (p *Provisioner) LoginAppServiceUser(ctx context.Context, username string) (string, error) {
	return p.matrixOps.LoginUserViaAppService(ctx, username)
}

// LoginWithPassword obtains a fresh access token via the password
// login flow. Used by legacy_password when AS mode is disabled and
// the controller has the user's stored InitialPassword.
func (p *Provisioner) LoginWithPassword(ctx context.Context, username, password string) (string, error) {
	return p.matrixOps.LoginUser(ctx, username, password)
}

// =========================================================================
// Composite wrappers retained for incremental migration
// =========================================================================
//
// EnsureHumanUser and LoginAsHuman remain as backward-compatible
// shims over the new primitives. In-tree callers that need
// per-identity-type behaviour migrate to the primitives directly via
// the humanidentity registry (see internal/controller/humanidentity).
// The wrappers are kept so the WorkerProvisioner / HumanProvisioner
// interface contracts stay stable for the team-admin login path and
// the existing mock-driven tests.
//
// IMPORTANT (P0-2 legacy fix): the AS branch below now calls
// SetUserPassword **only** when RegisterAppServiceUser actually
// created a new account. The previous implementation reset the
// password on every reconcile that hit this method, which would
// silently overwrite any password the user had rotated via Element
// the moment the controller decided to "re-provision".

// EnsureHumanUser registers (or logs in) a Matrix account for a Human CR.
// See HumanProvisioner.EnsureHumanUser for the contract around when this
// must be called. This implementation now routes through the explicit
// register / set-password primitives so the "set password" side effect
// is only triggered on first creation.
func (p *Provisioner) EnsureHumanUser(ctx context.Context, username string) (*HumanCredentials, error) {
	if p.MatrixAppServiceEnabled() {
		creds, err := p.RegisterAppServiceUser(ctx, username)
		if err != nil {
			return nil, fmt.Errorf("ensure human AS user %s: %w", username, err)
		}
		// Only assign an initial password on first registration. When
		// the account already existed (Created=false) we return the
		// AS-issued token without resetting whatever password the user
		// may have rotated via Element.
		if creds.Created {
			password, err := matrix.GeneratePassword(16)
			if err != nil {
				return nil, fmt.Errorf("generate human password: %w", err)
			}
			if err := p.SetUserPassword(ctx, creds.UserID, password); err != nil {
				return nil, fmt.Errorf("set human password via admin: %w", err)
			}
			creds.Password = password
		}
		return creds, nil
	}

	// Legacy path
	creds, err := p.RegisterLegacyUser(ctx, username)
	if err != nil {
		return nil, fmt.Errorf("ensure human matrix user %s: %w", username, err)
	}
	return creds, nil
}

// LoginAsHuman obtains a fresh access token for an already-provisioned
// Human without touching their password. This is the steady-state path
// the reconciler uses once Status.MatrixUserID is non-empty; it must NOT
// fall back to ProvisionUser on failure because the provider's
// password-reset fallback would silently overwrite any password the user
// changed via Element.
func (p *Provisioner) LoginAsHuman(ctx context.Context, username, password string) (string, error) {
	if p.MatrixAppServiceEnabled() {
		return p.LoginAppServiceUser(ctx, username)
	}
	return p.LoginWithPassword(ctx, username, password)
}

// =========================================================================
// Other Matrix-side operations Humans need (unchanged)
// =========================================================================

// SetDisplayName updates the Matrix profile displayname for a human user.
func (p *Provisioner) SetDisplayName(ctx context.Context, userID, accessToken, displayName string) error {
	return p.matrixOps.SetUserDisplayName(ctx, userID, accessToken, displayName)
}

// InviteToRoom invites the given Matrix user into roomID using the admin
// access token. Idempotent; see matrix.Client.InviteToRoom.
func (p *Provisioner) InviteToRoom(ctx context.Context, roomID, userID string) error {
	return p.matrixOps.AddMember(ctx, roomID, userID)
}

// JoinRoomAs joins roomID with the supplied user access token. Required
// because the controller-created rooms use the trusted_private_chat preset
// (per the Matrix spec), which leaves an invite pending until the invitee
// explicitly /joins — an admin-side invite alone is not sufficient to make
// the user a full member.
func (p *Provisioner) JoinRoomAs(ctx context.Context, roomID, userToken string) error {
	return p.matrixOps.JoinRoom(ctx, roomID, matrix.MemberSpec{ActorToken: userToken})
}

// KickFromRoom removes userID from roomID using the admin token. Idempotent.
func (p *Provisioner) KickFromRoom(ctx context.Context, roomID, userID, reason string) error {
	return p.matrixOps.RemoveMember(ctx, roomID, userID, reason)
}

// ForceLeaveRoom removes userID out of roomID even when a normal admin kick
// is not possible (e.g. the controller no longer holds a valid user token or
// the room power levels block the kick). Delegates to MatrixOps.RemoveMember,
// which tries the admin kick first and falls back to the provider-specific
// escalation (Tuwunel admin bot force-leave, Synapse make_room_admin + kick
// retry).
func (p *Provisioner) ForceLeaveRoom(ctx context.Context, userID, roomID string) error {
	log.FromContext(ctx).Info("force-leaving user from room", "room", roomID, "user", userID)
	return p.matrixOps.RemoveMember(ctx, roomID, userID, "force leave by admin")
}

// LeaveManagerRoom makes the Manager leave roomID using the Manager's own
// token. Used as a fallback when kicking the Manager out of a team-worker
// personal room 403s: worker rooms grant the Manager and the admin the same
// power level (both 100), so an admin kick is rejected by Matrix ("cannot
// kick user") and even the Synapse make_room_admin escalation cannot exceed
// the Manager's level. The Manager leaving itself avoids the power-level
// dependency entirely.
func (p *Provisioner) LeaveManagerRoom(ctx context.Context, roomID string) error {
	logger := log.FromContext(ctx)
	token := ""
	for _, key := range []string{"default", "manager"} {
		creds, err := p.creds.Load(ctx, key)
		if err == nil && creds != nil && creds.MatrixToken != "" {
			token = creds.MatrixToken
			break
		}
	}
	if token == "" {
		refresh, err := p.RefreshManagerCredentials(ctx, "default")
		if err != nil {
			return fmt.Errorf("refresh manager credentials: %w", err)
		}
		token = refresh.MatrixToken
	}
	if token == "" {
		return fmt.Errorf("manager access token unavailable")
	}
	logger.Info("manager leaving room via own token", "room", roomID)
	return p.matrixOps.LeaveRoom(ctx, roomID, matrix.MemberSpec{
		UserID:     p.matrixOps.UserIDFor("manager"),
		ActorToken: token,
	})
}

// DeactivateHumanUser disables a Matrix account via MatrixOps.DeactivateUser.
// The provider-specific admin operation (Tuwunel admin bot, Synapse admin
// REST) is fire-and-forget; the controller treats a successful call as the
// offboard handoff point.
func (p *Provisioner) DeactivateHumanUser(ctx context.Context, userID string) error {
	log.FromContext(ctx).Info("deactivating human matrix user", "user", userID)
	return p.matrixOps.DeactivateUser(ctx, userID)
}
