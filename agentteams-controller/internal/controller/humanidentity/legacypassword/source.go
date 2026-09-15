package legacypassword

import (
	"context"

	v1beta1 "github.com/agentscope-ai/AgentTeams/agentteams-controller/api/v1beta1"
	"github.com/agentscope-ai/AgentTeams/agentteams-controller/internal/controller/humanidentity"
	"github.com/agentscope-ai/AgentTeams/agentteams-controller/internal/service"
)

type source struct {
	deps humanidentity.Deps
}

func init() {
	humanidentity.Register(humanidentity.KeyLegacyPassword, func(deps humanidentity.Deps) humanidentity.IdentitySource {
		return source{deps: deps}
	})
}

func (s source) Key() string {
	return humanidentity.KeyLegacyPassword
}

func (s source) DeriveMatrixUserID(spec *v1beta1.HumanSpec, metadataName string) (string, error) {
	return s.deps.Provisioner.MatrixUserID(spec.EffectiveUsername(metadataName)), nil
}

func (s source) EnsurePrecreated(ctx context.Context, spec *v1beta1.HumanSpec, metadataName string) (humanidentity.Credentials, error) {
	return s.ensurePrecreated(ctx, spec, metadataName, "")
}

func (s source) EnsurePrecreatedWithOptions(ctx context.Context, spec *v1beta1.HumanSpec, metadataName string, opts humanidentity.EnsurePrecreatedOptions) (humanidentity.Credentials, error) {
	return s.ensurePrecreated(ctx, spec, metadataName, opts.DeviceID)
}

func (s source) ensurePrecreated(ctx context.Context, spec *v1beta1.HumanSpec, metadataName, deviceID string) (humanidentity.Credentials, error) {
	var creds *service.HumanCredentials
	var err error
	if deviceAware, ok := s.deps.Provisioner.(interface {
		EnsureHumanUserWithOptions(context.Context, string, string, string) (*service.HumanCredentials, error)
	}); ok {
		creds, err = deviceAware.EnsureHumanUserWithOptions(ctx, spec.EffectiveUsername(metadataName), spec.InitialPassword, deviceID)
	} else {
		creds, err = s.deps.Provisioner.EnsureHumanUser(ctx, spec.EffectiveUsername(metadataName))
	}
	if err != nil {
		return humanidentity.Credentials{}, err
	}
	// When the user pinned a custom initial password in spec, enforce it as
	// the Matrix password. This runs inside needsProvision only (first
	// registration or identity switch), so it never resets a password the
	// user has since rotated via Element. On the AS path EnsureHumanUser may
	// already have assigned a generated password for a brand-new account;
	// this override simply replaces it with the pinned value.
	if spec.InitialPassword != "" && creds.Password != spec.InitialPassword {
		if err := s.deps.Provisioner.SetUserPassword(ctx, creds.UserID, spec.InitialPassword); err != nil {
			return humanidentity.Credentials{}, err
		}
		creds.Password = spec.InitialPassword
	}
	return humanidentity.Credentials{
		UserID:      creds.UserID,
		AccessToken: creds.AccessToken,
		Password:    creds.Password,
		Created:     creds.Created,
	}, nil
}

func (s source) ManagesInitialPassword() bool {
	return true
}

func (s source) EnsureUserToken(ctx context.Context, spec *v1beta1.HumanSpec, status *v1beta1.HumanStatus, metadataName string) (string, error) {
	username := spec.EffectiveUsername(metadataName)
	if s.deps.Provisioner.MatrixAppServiceEnabled() {
		return s.deps.Provisioner.LoginAppServiceUser(ctx, username)
	}
	if status.InitialPassword == "" {
		return "", nil
	}
	return s.deps.Provisioner.LoginWithPassword(ctx, username, status.InitialPassword)
}

func (s source) EnsureDeactivated(context.Context, *v1beta1.HumanSpec, *v1beta1.HumanStatus) error {
	return nil
}
