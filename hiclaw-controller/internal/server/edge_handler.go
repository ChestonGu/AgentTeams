package server

import (
	"encoding/json"
	"log"
	"net/http"

	v1beta1 "github.com/hiclaw/hiclaw-controller/api/v1beta1"
	"github.com/hiclaw/hiclaw-controller/internal/backend"
	"github.com/hiclaw/hiclaw-controller/internal/httputil"
	"github.com/hiclaw/hiclaw-controller/internal/service"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// EdgeWorkerHandler handles edge worker registration and removal for teams.
type EdgeWorkerHandler struct {
	k8s         client.Client
	provisioner service.WorkerProvisioner
	deployer    service.WorkerDeployer
	backend     *backend.Registry
	namespace   string
}

// NewEdgeWorkerHandler creates a handler. provisioner/deployer may be nil
// (returns 501 for registration attempts).
func NewEdgeWorkerHandler(
	c client.Client,
	prov service.WorkerProvisioner,
	dep service.WorkerDeployer,
	b *backend.Registry,
	namespace string,
) *EdgeWorkerHandler {
	return &EdgeWorkerHandler{
		k8s:         c,
		provisioner: prov,
		deployer:    dep,
		backend:     b,
		namespace:   namespace,
	}
}

// RegisterEdgeWorkerRequest is the body for POST /api/v1/teams/{team}/edge-workers.
type RegisterEdgeWorkerRequest struct {
	Name       string  `json:"name"`
	WorkerName string  `json:"workerName,omitempty"`
	Model      string  `json:"model"`
	Runtime    string  `json:"runtime,omitempty"`
	Skills     []string `json:"skills,omitempty"`
}

// RegisterEdgeWorkerResponse is returned on successful registration.
type RegisterEdgeWorkerResponse struct {
	Name           string `json:"name"`
	MatrixUserID   string `json:"matrixUserID"`
	MatrixToken    string `json:"matrixToken"`
	MatrixPassword string `json:"matrixPassword"`
	GatewayKey     string `json:"gatewayKey"`
	RoomID         string `json:"roomID"`
	TeamRoomID     string `json:"teamRoomID"`
	Message        string `json:"message"`
}

// Register handles POST /api/v1/teams/{team}/edge-workers.
// Synchronously provisions Matrix identity, Gateway consumer, config to MinIO,
// and adds the edge worker to the team spec.
func (h *EdgeWorkerHandler) Register(w http.ResponseWriter, r *http.Request) {
	teamName := r.PathValue("team")
	if teamName == "" {
		httputil.WriteError(w, http.StatusBadRequest, "team name is required")
		return
	}
	if h.provisioner == nil || h.deployer == nil {
		httputil.WriteError(w, http.StatusNotImplemented, "edge worker registration requires provisioner and deployer")
		return
	}

	var req RegisterEdgeWorkerRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		httputil.WriteError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
		return
	}
	if req.Name == "" {
		httputil.WriteError(w, http.StatusBadRequest, "name is required")
		return
	}
	if req.Model == "" {
		httputil.WriteError(w, http.StatusBadRequest, "model is required")
		return
	}

	ctx := r.Context()

	// 1. Get team
	var team v1beta1.Team
	if err := h.k8s.Get(ctx, client.ObjectKey{Name: teamName, Namespace: h.namespace}, &team); err != nil {
		writeK8sError(w, "get team", err)
		return
	}

	// 2. Check if already a member
	for _, ew := range team.Spec.Workers {
		if ew.Name == req.Name {
			httputil.WriteError(w, http.StatusConflict, "worker already exists in team")
			return
		}
	}

	// 3. Add to team spec (containerManaged=false)
	zero := false
	team.Spec.Workers = append(team.Spec.Workers, v1beta1.TeamWorkerSpec{
		Name:             req.Name,
		WorkerName:       req.WorkerName,
		Model:            req.Model,
		Runtime:          req.Runtime,
		Skills:           req.Skills,
		ContainerManaged: &zero,
	})
	if err := h.k8s.Update(ctx, &team); err != nil {
		writeK8sError(w, "add edge worker to team spec", err)
		return
	}

	// 4. Provision Matrix identity + Gateway consumer + room
	leaderName := team.Spec.Leader.EffectiveWorkerName()
	runtimeName := req.Name
	if req.WorkerName != "" {
		runtimeName = req.WorkerName
	}
	provResult, err := h.provisioner.ProvisionWorker(ctx, service.WorkerProvisionRequest{
		Name:           runtimeName,
		CredentialName: req.Name,
		Role:           "worker",
		TeamName:       team.Spec.EffectiveTeamName(teamName),
		TeamLeaderName: leaderName,
	})
	if err != nil {
		log.Printf("[WARN] edge worker provision failed for %s: %v (team spec already updated, reconciler will retry)", req.Name, err)
		httputil.WriteError(w, http.StatusInternalServerError, "provision failed: "+err.Error())
		return
	}

	// 5. Generate and push config to MinIO
	channelPolicy := &v1beta1.ChannelPolicySpec{
		GroupAllowExtra: []string{leaderName},
	}
	if team.Spec.Admin != nil && team.Spec.Admin.MatrixUserID != "" {
		channelPolicy.GroupAllowExtra = append(channelPolicy.GroupAllowExtra, team.Spec.Admin.MatrixUserID)
	}
	for _, m := range team.Spec.HumanMembers {
		if m.MatrixUserID != "" {
			channelPolicy.GroupAllowExtra = append(channelPolicy.GroupAllowExtra, m.MatrixUserID)
		}
	}

	spec := v1beta1.WorkerSpec{
		Model:            req.Model,
		Runtime:          req.Runtime,
		Skills:           req.Skills,
		ContainerManaged: &zero,
		ChannelPolicy:    channelPolicy,
	}
	if err := h.deployer.DeployWorkerConfig(ctx, service.WorkerDeployRequest{
		Name:           runtimeName,
		Spec:           spec,
		Role:           "worker",
		TeamName:       team.Spec.EffectiveTeamName(teamName),
		TeamLeaderName: leaderName,
		MatrixToken:    provResult.MatrixToken,
		GatewayKey:     provResult.GatewayKey,
		MatrixPassword: provResult.MatrixPassword,
	}); err != nil {
		log.Printf("[WARN] edge worker config deploy failed for %s: %v", req.Name, err)
	}

	httputil.WriteJSON(w, http.StatusCreated, RegisterEdgeWorkerResponse{
		Name:           req.Name,
		MatrixUserID:   provResult.MatrixUserID,
		MatrixToken:    provResult.MatrixToken,
		MatrixPassword: provResult.MatrixPassword,
		GatewayKey:     provResult.GatewayKey,
		RoomID:         provResult.RoomID,
		TeamRoomID:     team.Status.TeamRoomID,
		Message:        "registered as edge worker in team " + teamName,
	})
}

// RemoveEdgeWorker handles DELETE /api/v1/teams/{team}/edge-workers/{member}.
// Synchronously deprovisions the edge worker and removes it from the team spec.
func (h *EdgeWorkerHandler) RemoveEdgeWorker(w http.ResponseWriter, r *http.Request) {
	teamName := r.PathValue("team")
	memberName := r.PathValue("member")
	if teamName == "" || memberName == "" {
		httputil.WriteError(w, http.StatusBadRequest, "team and member name are required")
		return
	}
	if h.provisioner == nil {
		httputil.WriteError(w, http.StatusNotImplemented, "edge worker removal requires provisioner")
		return
	}

	ctx := r.Context()

	// 1. Get team
	var team v1beta1.Team
	if err := h.k8s.Get(ctx, client.ObjectKey{Name: teamName, Namespace: h.namespace}, &team); err != nil {
		writeK8sError(w, "get team", err)
		return
	}

	// 2. Find and verify it's an edge worker
	found := false
	for _, ew := range team.Spec.Workers {
		if ew.Name == memberName {
			if ew.DesiredContainerMan() {
				httputil.WriteError(w, http.StatusBadRequest, "cannot remove container-managed worker via edge-worker API; update team spec directly")
				return
			}
			found = true
			break
		}
	}
	if !found {
		httputil.WriteError(w, http.StatusNotFound, "edge worker not found in team")
		return
	}

	// 3. Deprovision infra
	runtimeName := memberName
	if ms := team.Status.MemberByName(memberName); ms != nil && ms.RuntimeName != "" {
		runtimeName = ms.RuntimeName
	}

	if err := h.provisioner.LeaveAllWorkerRooms(ctx, runtimeName); err != nil {
		log.Printf("[WARN] edge worker leave-all-rooms failed for %s: %v", memberName, err)
	}
	if ms := team.Status.MemberByName(memberName); ms != nil && ms.RoomID != "" {
		if err := h.provisioner.DeleteWorkerRoom(ctx, ms.RoomID); err != nil {
			log.Printf("[WARN] edge worker room delete failed for %s: %v", memberName, err)
		}
	}
	if err := h.provisioner.DeprovisionWorker(ctx, service.WorkerDeprovisionRequest{
		Name:         runtimeName,
		IsTeamWorker: true,
	}); err != nil {
		log.Printf("[WARN] edge worker deprovision failed for %s: %v", memberName, err)
	}
	if h.deployer != nil {
		if err := h.deployer.CleanupOSSData(ctx, runtimeName); err != nil {
			log.Printf("[WARN] edge worker OSS cleanup failed for %s: %v", memberName, err)
		}
	}
	_ = h.provisioner.DeleteServiceAccount(ctx, memberName)
	_ = h.provisioner.DeleteWorkerRoomAlias(ctx, runtimeName)

	// 4. Remove from team spec
	for i, ew := range team.Spec.Workers {
		if ew.Name == memberName {
			team.Spec.Workers = append(team.Spec.Workers[:i], team.Spec.Workers[i+1:]...)
			break
		}
	}
	if err := h.k8s.Update(ctx, &team); err != nil {
		if apierrors.IsConflict(err) {
			httputil.WriteError(w, http.StatusConflict, "team spec was modified concurrently; retry")
			return
		}
		writeK8sError(w, "update team spec", err)
		return
	}

	httputil.WriteJSON(w, http.StatusOK, map[string]string{
		"name":    memberName,
		"message": "edge worker removed from team " + teamName,
	})
}
