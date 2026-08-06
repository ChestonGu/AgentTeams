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

// MakeRoomAdmin raises userID to the same power level as the room's highest
// current local admin in roomID via the Synapse admin API. It is the recovery
// path for in-room CS operations (invite/kick/state) that fail because the
// operator lacks power: after this call the operator's power level matches the
// room's top admin, so the CS retry succeeds.
//
// Note the actual API semantics (Synapse 1.127 rest/admin/rooms.py
// MakeRoomAdminRestServlet): the target is raised to the top local admin's PL
// — not necessarily 100 — and if the target is NOT joined the API only
// INVITES them; it does not force a join. Use ForceJoinRoom when the operator
// needs to actually be in the room.
//
//	POST /_synapse/admin/v1/rooms/{roomID}/make_room_admin
//	body: {"user_id": "<userID>"}
func (s *SynapseClient) MakeRoomAdmin(ctx context.Context, roomID, userID string) error {
	path := "/_synapse/admin/v1/rooms/" + url.PathEscape(roomID) + "/make_room_admin"
	return s.synAdminCall(ctx, http.MethodPost, path, map[string]string{"user_id": userID})
}

// ForceJoinRoom joins userID into roomID via the Synapse admin API, bypassing
// the room's join rules: public rooms are joined directly; non-public rooms
// get an auto-invite followed by an immediate join. It is the Synapse-native
// recovery for CS operations that fail with "not in room" — the operator's
// membership is restored by the server instead of a Tuwunel-style
// force-leave/rejoin dance. Idempotent: an already-joined user returns 200.
//
//	POST /_synapse/admin/v1/join/{roomID}
//	body: {"user_id": "<userID>"}
//
// The caller must be a server admin (cached admin token via synAdminCall);
// userID must be a local user.
func (s *SynapseClient) ForceJoinRoom(ctx context.Context, roomID, userID string) error {
	path := "/_synapse/admin/v1/join/" + url.PathEscape(roomID)
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
