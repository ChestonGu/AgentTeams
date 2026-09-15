package server

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"regexp"

	v1beta1 "github.com/agentscope-ai/AgentTeams/agentteams-controller/api/v1beta1"
	authpkg "github.com/agentscope-ai/AgentTeams/agentteams-controller/internal/auth"
	"github.com/agentscope-ai/AgentTeams/agentteams-controller/internal/controller/humanidentity"
	_ "github.com/agentscope-ai/AgentTeams/agentteams-controller/internal/controller/humanidentity/externalsso"
	_ "github.com/agentscope-ai/AgentTeams/agentteams-controller/internal/controller/humanidentity/legacypassword"
	"github.com/agentscope-ai/AgentTeams/agentteams-controller/internal/httputil"
	"github.com/agentscope-ai/AgentTeams/agentteams-controller/internal/service"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"
)

var deviceIDPattern = regexp.MustCompile(`^[A-Za-z0-9._~-]{1,255}$`)

type HumanLoginRequest struct {
	Name            string `json:"name"`
	DisplayName     string `json:"displayName"`
	Email           string `json:"email,omitempty"`
	InitialPassword string `json:"initialPassword,omitempty"`
	DeviceID        string `json:"deviceId,omitempty"`
}

type HumanLoginResponse struct {
	Name         string `json:"name"`
	Created      bool   `json:"created"`
	MatrixUserID string `json:"matrixUserID"`
	AccessToken  string `json:"accessToken"`
	Phase        string `json:"phase"`
}

// HumanLoginHandler owns the imperative Human login/provisioning action. It
// deliberately never writes Human status; the reconciler remains the status
// single writer.
type HumanLoginHandler struct {
	client         client.Client
	namespace      string
	controllerName string
	provisioner    service.HumanProvisioner
}

func NewHumanLoginHandler(c client.Client, namespace, controllerName string, provisioner service.HumanProvisioner) *HumanLoginHandler {
	return &HumanLoginHandler{client: c, namespace: namespace, controllerName: controllerName, provisioner: provisioner}
}

func (h *HumanLoginHandler) Login(w http.ResponseWriter, r *http.Request) {
	auditResult := "failed"
	created := false
	auditTarget := ""
	defer func() {
		caller := authpkg.CallerFromContext(r.Context())
		fields := []interface{}{"operation", "human-sso-login", "targetHuman", auditTarget, "result", auditResult, "created", created}
		if caller != nil {
			fields = append(fields, "caller", caller.Username, "callerRole", caller.Role)
		}
		log.FromContext(r.Context()).WithName("human-login").Info("human login action", fields...)
	}()
	var req HumanLoginRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		httpError(w, http.StatusBadRequest, "InvalidRequest", "invalid JSON: "+err.Error())
		return
	}
	if req.Name == "" {
		httpError(w, http.StatusBadRequest, "InvalidRequest", "name is required")
		return
	}
	auditTarget = req.Name
	if req.DeviceID != "" && !deviceIDPattern.MatchString(req.DeviceID) {
		httpError(w, http.StatusBadRequest, "InvalidDeviceID", "deviceId contains invalid characters or length")
		return
	}
	if h.provisioner == nil {
		httpError(w, http.StatusServiceUnavailable, "ProvisionerUnavailable", "human provisioner is not available")
		return
	}
	if h.provisioner.MatrixAppServiceEnabled() {
		httpError(w, http.StatusNotImplemented, "UnsupportedIdentityMode", "sso-login currently requires legacy_password mode")
		return
	}

	ctx := r.Context()
	var human v1beta1.Human
	err := h.client.Get(ctx, client.ObjectKey{Name: req.Name, Namespace: h.namespace}, &human)
	if apierrors.IsNotFound(err) {
		if req.InitialPassword == "" {
			httpError(w, http.StatusBadRequest, "InitialPasswordRequired", "initialPassword is required for a new human")
			return
		}
		human = v1beta1.Human{
			ObjectMeta: metav1.ObjectMeta{Name: req.Name, Namespace: h.namespace},
			Spec: v1beta1.HumanSpec{
				DisplayName:     req.DisplayName,
				Email:           req.Email,
				InitialPassword: req.InitialPassword,
			},
		}
		if h.controllerName != "" {
			human.Labels = map[string]string{v1beta1.LabelController: h.controllerName}
		}
		if err := h.client.Create(ctx, &human); err != nil {
			if !apierrors.IsAlreadyExists(err) {
				writeK8sError(w, "create human for login", err)
				return
			}
			// Another request won the create race. Re-read from the configured
			// client and continue through the existing-user path.
			if err := h.client.Get(ctx, client.ObjectKey{Name: req.Name, Namespace: h.namespace}, &human); err != nil {
				writeK8sError(w, "get human after create race", err)
				return
			}
		} else {
			created = true
			humanidentityResult, err := h.ensurePrecreated(ctx, &human, req.DeviceID)
			if err != nil {
				httpError(w, http.StatusServiceUnavailable, "ProvisioningFailed", "human provisioning is temporarily unavailable")
				return
			}
			httpLoginResponse(w, HumanLoginResponse{
				Name: human.Name, Created: true, MatrixUserID: humanidentityResult.UserID,
				AccessToken: humanidentityResult.AccessToken, Phase: humanPhase(&human),
			})
			auditResult = "success"
			return
		}
	}
	if err != nil && !apierrors.IsNotFound(err) {
		writeK8sError(w, "get human for login", err)
		return
	}
	if human.Spec.IdentitySource != nil {
		httpError(w, http.StatusNotImplemented, "UnsupportedIdentityMode", "sso-login currently supports legacy_password humans only")
		return
	}

	pinnedPassword := human.Status.InitialPassword
	if pinnedPassword == "" {
		pinnedPassword = human.Spec.InitialPassword
	}
	if pinnedPassword == "" {
		httpError(w, http.StatusTooEarly, "CredentialNotReady", "human credentials are not ready")
		return
	}
	if req.InitialPassword != "" && req.InitialPassword != pinnedPassword {
		httpError(w, http.StatusConflict, "PasswordMismatch", "request password does not match the pinned credential")
		return
	}
	username := human.Spec.EffectiveUsername(human.Name)
	token, err := h.provisioner.LoginWithPasswordAndOptions(ctx, username, pinnedPassword, req.DeviceID)
	if err != nil {
		httpError(w, http.StatusUnauthorized, "MatrixLoginFailed", "matrix login failed")
		return
	}
	httpLoginResponse(w, HumanLoginResponse{
		Name: human.Name, Created: false, MatrixUserID: human.Status.MatrixUserID,
		AccessToken: token, Phase: humanPhase(&human),
	})
	auditResult = "success"
}

func (h *HumanLoginHandler) ensurePrecreated(ctx context.Context, human *v1beta1.Human, deviceID string) (humanidentity.Credentials, error) {
	identity, err := humanidentity.ResolveHuman(&human.Spec, human.Name, humanidentity.Deps{Provisioner: h.provisioner})
	if err != nil {
		return humanidentity.Credentials{}, err
	}
	if identity.Source.Key() != humanidentity.KeyLegacyPassword {
		return humanidentity.Credentials{}, fmt.Errorf("sso-login requires legacy_password identity source")
	}
	if deviceAware, ok := identity.Source.(humanidentity.DeviceAwareIdentitySource); ok {
		return deviceAware.EnsurePrecreatedWithOptions(ctx, &human.Spec, human.Name, humanidentity.EnsurePrecreatedOptions{DeviceID: deviceID})
	}
	return identity.Source.EnsurePrecreated(ctx, &human.Spec, human.Name)
}

func humanPhase(human *v1beta1.Human) string {
	if human.Status.Phase == "" {
		return "Pending"
	}
	return human.Status.Phase
}

func httpLoginResponse(w http.ResponseWriter, response HumanLoginResponse) {
	httputil.WriteJSON(w, http.StatusOK, response)
}

func httpError(w http.ResponseWriter, status int, code, message string) {
	httputil.WriteJSON(w, status, map[string]string{"code": code, "message": message})
}
