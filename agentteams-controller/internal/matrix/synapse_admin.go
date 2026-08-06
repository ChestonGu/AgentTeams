package matrix

import (
	"context"
	"net/http"
	"net/url"
)

// This file holds the Synapse-specific admin REST API client methods used by
// SynapseMatrixOps. They sit on *SynapseClient next to synAdminCall (defined
// in synapse_client.go), which issues the request with the cached admin token
// and treats 2xx as success. The admin account (AGENTTEAMS_ADMIN_USER) must
// be a Synapse server admin, or these endpoints return 403.

// MakeRoomAdmin grants userID full room-admin power (power level 100) in
// roomID via the Synapse admin API. It is the recovery path for in-room CS
// operations (invite/kick/state) that fail because the operator is not joined
// or lacks power level: after this call the operator is joined AND has PL=100,
// so the CS retry succeeds.
//
//	POST /_synapse/admin/v1/rooms/{roomID}/make_room_admin
//	body: {"user_id": "<userID>"}
func (s *SynapseClient) MakeRoomAdmin(ctx context.Context, roomID, userID string) error {
	path := "/_synapse/admin/v1/rooms/" + url.PathEscape(roomID) + "/make_room_admin"
	return s.synAdminCall(ctx, http.MethodPost, path, map[string]string{"user_id": userID})
}

// DeleteRoom shuts down and purges roomID via the Synapse admin API.
// Fire-and-forget: Synapse performs the purge asynchronously (the v2 delete
// API returns a purge id and runs in the background).
//
//	DELETE /_synapse/admin/v2/rooms/{roomID}
func (s *SynapseClient) DeleteRoom(ctx context.Context, roomID string) error {
	path := "/_synapse/admin/v2/rooms/" + url.PathEscape(roomID)
	return s.synAdminCall(ctx, http.MethodDelete, path, nil)
}
