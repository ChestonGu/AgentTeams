package matrix

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// These tests exercise SynapseMatrixOps through a mocked Synapse homeserver
// (httptest). They verify the Synapse-specific behaviors documented in
// design/synapse-interface-contracts.md: creator auto-injection on
// CreateRoom (§4 修复 4), make_room_admin fallback on invite/kick
// (event_auth.py:687/:703/:717), precise kick idempotency (room_member.py:1022),
// and v2 admin room deletion (§3).

func TestSynapseOps_CreateRoom_AutoInjectsCreator(t *testing.T) {
	var captured map[string]interface{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/_matrix/client/v3/login":
			adminLoginHandler(t, w)
		case "/_matrix/client/v3/createRoom":
			var body map[string]interface{}
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Fatalf("decode createRoom body: %v", err)
			}
			captured = body
			w.WriteHeader(http.StatusOK)
			json.NewEncoder(w).Encode(map[string]string{"room_id": "!worker:d"})
		default:
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()

	ops := NewSynapseMatrixOps(Config{
		ServerURL: server.URL, Domain: "d", AdminUser: "admin", AdminPassword: "pw",
	}, server.Client())

	ref, err := ops.CreateRoom(context.Background(), RoomSpec{
		Name:           "Alice Room",
		AliasLocalpart: "alice-worker",
		// Deliberately omit the creator (admin) from PowerLevels — Synapse
		// 1.127 rejects power_level_content_override whose "users" omits the
		// creator (room.py:878-888), so the ops layer must inject admin=100.
		PowerLevels: map[string]int{"@alice:d": 0},
	})
	if err != nil {
		t.Fatalf("CreateRoom: %v", err)
	}
	if ref.RoomID != "!worker:d" || !ref.Created {
		t.Errorf("RoomRef = %+v, want RoomID=!worker:d Created=true", ref)
	}

	pl, ok := captured["power_level_content_override"].(map[string]interface{})
	if !ok {
		t.Fatalf("missing power_level_content_override in %v", captured)
	}
	users, ok := pl["users"].(map[string]interface{})
	if !ok {
		t.Fatalf("missing users in power_level_content_override")
	}
	if users["@admin:d"] != float64(100) {
		t.Errorf("injected creator PL = %v, want 100", users["@admin:d"])
	}
	if users["@alice:d"] != float64(0) {
		t.Errorf("existing PL = %v, want 0", users["@alice:d"])
	}
}

func TestSynapseOps_CreateRoom_ActorPLInjection(t *testing.T) {
	var captured map[string]interface{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/_matrix/client/v3/createRoom":
			if auth := r.Header.Get("Authorization"); auth != "Bearer team-admin-token" {
				t.Errorf("Authorization = %q, want Bearer team-admin-token", auth)
			}
			var body map[string]interface{}
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Fatalf("decode createRoom body: %v", err)
			}
			captured = body
			w.WriteHeader(http.StatusOK)
			json.NewEncoder(w).Encode(map[string]string{"room_id": "!team:d"})
		default:
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()

	ops := NewSynapseMatrixOps(Config{
		ServerURL: server.URL, Domain: "d", AdminUser: "admin", AdminPassword: "pw",
	}, server.Client())

	// Team room: creator is the team admin, who is already in PowerLevels —
	// so Synapse must NOT inject the global admin (the pre-abstraction test
	// contract asserts the team room never contains the global admin).
	_, err := ops.CreateRoom(context.Background(), RoomSpec{
		Name:           "Team Room",
		AliasLocalpart: "team-alpha",
		ActorUserID:    "@team-admin:d",
		ActorToken:     "team-admin-token",
		PowerLevels:    map[string]int{"@team-admin:d": 100, "@manager:d": 100},
	})
	if err != nil {
		t.Fatalf("CreateRoom: %v", err)
	}

	pl, ok := captured["power_level_content_override"].(map[string]interface{})
	if !ok {
		t.Fatalf("missing power_level_content_override in %v", captured)
	}
	users, ok := pl["users"].(map[string]interface{})
	if !ok {
		t.Fatalf("missing users in power_level_content_override")
	}
	if _, ok := users["@admin:d"]; ok {
		t.Errorf("global admin must not be injected into team room PowerLevels: %v", users)
	}
	if users["@team-admin:d"] != float64(100) {
		t.Errorf("actor PL = %v, want 100", users["@team-admin:d"])
	}
}

func TestSynapseOps_AddMember_FallbackViaMakeRoomAdmin(t *testing.T) {
	var (
		inviteCalls    int
		makeAdminCalls int
	)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/_matrix/client/v3/login":
			adminLoginHandler(t, w)
		case "/_matrix/client/v3/rooms/!room:d/invite":
			inviteCalls++
			if inviteCalls == 1 {
				// Synapse 1.127 event_auth.py:687 — sender not joined.
				w.WriteHeader(http.StatusForbidden)
				json.NewEncoder(w).Encode(map[string]string{
					"errcode": "M_FORBIDDEN",
					"error":   "@admin:d not in room !room:d.",
				})
				return
			}
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("{}"))
		case "/_synapse/admin/v1/rooms/!room:d/make_room_admin":
			makeAdminCalls++
			var body map[string]string
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Fatalf("decode make_room_admin body: %v", err)
			}
			if body["user_id"] != "@admin:d" {
				t.Errorf("make_room_admin user_id = %q, want @admin:d", body["user_id"])
			}
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("{}"))
		default:
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()

	ops := NewSynapseMatrixOps(Config{
		ServerURL: server.URL, Domain: "d", AdminUser: "admin", AdminPassword: "pw",
	}, server.Client())

	if err := ops.AddMember(context.Background(), "!room:d", "@alice:d"); err != nil {
		t.Fatalf("AddMember with fallback: %v", err)
	}
	if inviteCalls != 2 {
		t.Errorf("invite calls = %d, want 2 (initial + retry)", inviteCalls)
	}
	if makeAdminCalls != 1 {
		t.Errorf("make_room_admin calls = %d, want 1", makeAdminCalls)
	}
}

func TestSynapseOps_RemoveMember_Idempotent_TargetNotInRoom(t *testing.T) {
	makeAdminCalled := false
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/_matrix/client/v3/login":
			adminLoginHandler(t, w)
		case "/_matrix/client/v3/rooms/!room:d/kick":
			// Synapse 1.127 room_member.py:1022/1039 — target not in room.
			w.WriteHeader(http.StatusForbidden)
			json.NewEncoder(w).Encode(map[string]string{
				"errcode": "M_FORBIDDEN",
				"error":   "The target user is not in the room",
			})
		case "/_synapse/admin/v1/rooms/!room:d/make_room_admin":
			makeAdminCalled = true
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("{}"))
		default:
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()

	ops := NewSynapseMatrixOps(Config{
		ServerURL: server.URL, Domain: "d", AdminUser: "admin", AdminPassword: "pw",
	}, server.Client())

	if err := ops.RemoveMember(context.Background(), "!room:d", "@alice:d", ""); err != nil {
		t.Errorf("expected nil for target-not-in-room, got %v", err)
	}
	if makeAdminCalled {
		t.Error("make_room_admin must not be called for target-not-in-room idempotency")
	}
}

func TestSynapseOps_RemoveMember_FallbackViaMakeRoomAdmin(t *testing.T) {
	var (
		kickCalls      int
		makeAdminCalls int
	)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/_matrix/client/v3/login":
			adminLoginHandler(t, w)
		case "/_matrix/client/v3/rooms/!room:d/kick":
			kickCalls++
			if kickCalls == 1 {
				// Synapse 1.127 event_auth.py:717 — insufficient power.
				w.WriteHeader(http.StatusForbidden)
				json.NewEncoder(w).Encode(map[string]string{
					"errcode": "M_FORBIDDEN",
					"error":   "You cannot kick user @alice:d.",
				})
				return
			}
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("{}"))
		case "/_synapse/admin/v1/rooms/!room:d/make_room_admin":
			makeAdminCalls++
			var body map[string]string
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Fatalf("decode make_room_admin body: %v", err)
			}
			if body["user_id"] != "@admin:d" {
				t.Errorf("make_room_admin user_id = %q, want @admin:d", body["user_id"])
			}
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("{}"))
		default:
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()

	ops := NewSynapseMatrixOps(Config{
		ServerURL: server.URL, Domain: "d", AdminUser: "admin", AdminPassword: "pw",
	}, server.Client())

	if err := ops.RemoveMember(context.Background(), "!room:d", "@alice:d", ""); err != nil {
		t.Fatalf("RemoveMember with fallback: %v", err)
	}
	if kickCalls != 2 {
		t.Errorf("kick calls = %d, want 2 (initial + retry)", kickCalls)
	}
	if makeAdminCalls != 1 {
		t.Errorf("make_room_admin calls = %d, want 1", makeAdminCalls)
	}
}

func TestSynapseOps_DissolveRoom_UsesV2Delete(t *testing.T) {
	deleteHit := false
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/_matrix/client/v3/login":
			adminLoginHandler(t, w)
		case "/_synapse/admin/v2/rooms/!room:d":
			deleteHit = true
			if r.Method != http.MethodDelete {
				t.Errorf("method = %s, want DELETE", r.Method)
			}
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("{}"))
		default:
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()

	ops := NewSynapseMatrixOps(Config{
		ServerURL: server.URL, Domain: "d", AdminUser: "admin", AdminPassword: "pw",
	}, server.Client())

	if err := ops.DissolveRoom(context.Background(), "!room:d"); err != nil {
		t.Fatalf("DissolveRoom: %v", err)
	}
	if !deleteHit {
		t.Error("DELETE /_synapse/admin/v2/rooms/!room:d was not called")
	}
}

// === Phase 3: Room Metadata & Messaging fallback + queries + HealthCheck ===

// TestSynapseOps_SetRoomMetadata_FallbackViaMakeRoomAdmin verifies that a
// room.meta write failing with sender-not-joined (event_auth.py:731) escalates
// to make_room_admin on the actor and retries the CS write with the same
// token.
func TestSynapseOps_SetRoomMetadata_FallbackViaMakeRoomAdmin(t *testing.T) {
	var (
		stateCalls     int
		makeAdminCalls int
	)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/_matrix/client/v3/login":
			adminLoginHandler(t, w)
		case "/_matrix/client/v3/rooms/!room:d/state/room.meta/":
			stateCalls++
			if auth := r.Header.Get("Authorization"); auth != "Bearer team-admin-token" {
				t.Errorf("Authorization = %q, want Bearer team-admin-token", auth)
			}
			if stateCalls == 1 {
				// Synapse 1.127 event_auth.py:731 — sender not joined.
				w.WriteHeader(http.StatusForbidden)
				json.NewEncoder(w).Encode(map[string]string{
					"errcode": "M_FORBIDDEN",
					"error":   "User @team-admin:d not in room !room:d (None)",
				})
				return
			}
			var body map[string]interface{}
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Fatalf("decode room.meta body: %v", err)
			}
			if body["roomKind"] != "team_room" {
				t.Errorf("room.meta content = %v, want roomKind=team_room", body)
			}
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("{}"))
		case "/_synapse/admin/v1/rooms/!room:d/make_room_admin":
			makeAdminCalls++
			var body map[string]string
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Fatalf("decode make_room_admin body: %v", err)
			}
			if body["user_id"] != "@team-admin:d" {
				t.Errorf("make_room_admin user_id = %q, want @team-admin:d (the actor)", body["user_id"])
			}
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("{}"))
		default:
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()

	ops := NewSynapseMatrixOps(Config{
		ServerURL: server.URL, Domain: "d", AdminUser: "admin", AdminPassword: "pw",
	}, server.Client())

	err := ops.SetRoomMetadata(context.Background(), "!room:d", map[string]interface{}{"roomKind": "team_room"},
		MemberSpec{ActorUserID: "@team-admin:d", ActorToken: "team-admin-token"})
	if err != nil {
		t.Fatalf("SetRoomMetadata with fallback: %v", err)
	}
	if stateCalls != 2 {
		t.Errorf("room.meta state calls = %d, want 2 (initial + retry)", stateCalls)
	}
	if makeAdminCalls != 1 {
		t.Errorf("make_room_admin calls = %d, want 1", makeAdminCalls)
	}
}

// TestSynapseOps_RenameRoom_FallbackViaMakeRoomAdmin verifies the
// insufficient-power string (event_auth.py:768) escalates to make_room_admin
// and the rename retries.
func TestSynapseOps_RenameRoom_FallbackViaMakeRoomAdmin(t *testing.T) {
	var (
		renameCalls    int
		makeAdminCalls int
	)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/_matrix/client/v3/login":
			adminLoginHandler(t, w)
		case "/_matrix/client/v3/rooms/!room:d/state/m.room.name/":
			renameCalls++
			if renameCalls == 1 {
				// Synapse 1.127 event_auth.py:768 — PL < send_level.
				w.WriteHeader(http.StatusForbidden)
				json.NewEncoder(w).Encode(map[string]string{
					"errcode": "M_FORBIDDEN",
					"error": "You don't have permission to post that to the room. " +
						"user_level (0) < send_level (50)",
				})
				return
			}
			var body map[string]string
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Fatalf("decode m.room.name body: %v", err)
			}
			if body["name"] != "New Name" {
				t.Errorf("name = %q, want New Name", body["name"])
			}
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("{}"))
		case "/_synapse/admin/v1/rooms/!room:d/make_room_admin":
			makeAdminCalls++
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("{}"))
		default:
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()

	ops := NewSynapseMatrixOps(Config{
		ServerURL: server.URL, Domain: "d", AdminUser: "admin", AdminPassword: "pw",
	}, server.Client())

	if err := ops.RenameRoom(context.Background(), "!room:d", "New Name", MemberSpec{}); err != nil {
		t.Fatalf("RenameRoom with fallback: %v", err)
	}
	if renameCalls != 2 {
		t.Errorf("rename calls = %d, want 2 (initial + retry)", renameCalls)
	}
	if makeAdminCalls != 1 {
		t.Errorf("make_room_admin calls = %d, want 1", makeAdminCalls)
	}
}

// TestSynapseOps_SendSystemMessage_FallbackViaMakeRoomAdmin verifies the
// system-message path (admin identity) escalates to make_room_admin when the
// admin is not joined, then retries the send.
func TestSynapseOps_SendSystemMessage_FallbackViaMakeRoomAdmin(t *testing.T) {
	var (
		sendCalls      int
		makeAdminCalls int
	)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/_matrix/client/v3/login":
			adminLoginHandler(t, w)
		case strings.HasPrefix(r.URL.Path, "/_matrix/client/v3/rooms/!room:d/send/m.room.message/"):
			sendCalls++
			if sendCalls == 1 {
				// Synapse 1.127 event_auth.py:731 — sender (admin) not joined.
				w.WriteHeader(http.StatusForbidden)
				json.NewEncoder(w).Encode(map[string]string{
					"errcode": "M_FORBIDDEN",
					"error":   "User @admin:d not in room !room:d (None)",
				})
				return
			}
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("{}"))
		case r.URL.Path == "/_synapse/admin/v1/rooms/!room:d/make_room_admin":
			makeAdminCalls++
			var body map[string]string
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Fatalf("decode make_room_admin body: %v", err)
			}
			if body["user_id"] != "@admin:d" {
				t.Errorf("make_room_admin user_id = %q, want @admin:d", body["user_id"])
			}
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("{}"))
		default:
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()

	ops := NewSynapseMatrixOps(Config{
		ServerURL: server.URL, Domain: "d", AdminUser: "admin", AdminPassword: "pw",
	}, server.Client())

	if err := ops.SendSystemMessage(context.Background(), "!room:d", "hello"); err != nil {
		t.Fatalf("SendSystemMessage with fallback: %v", err)
	}
	if sendCalls != 2 {
		t.Errorf("send calls = %d, want 2 (initial + retry)", sendCalls)
	}
	if makeAdminCalls != 1 {
		t.Errorf("make_room_admin calls = %d, want 1", makeAdminCalls)
	}
}

// TestSynapseOps_SetRoomMetadata_NoFallbackOnOtherError verifies that a
// non-fallback error (e.g. a 400 M_UNKNOWN) is returned as-is without
// touching make_room_admin.
func TestSynapseOps_SetRoomMetadata_NoFallbackOnOtherError(t *testing.T) {
	makeAdminCalled := false
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/_matrix/client/v3/login":
			adminLoginHandler(t, w)
		case "/_matrix/client/v3/rooms/!room:d/state/room.meta/":
			w.WriteHeader(http.StatusBadRequest)
			json.NewEncoder(w).Encode(map[string]string{
				"errcode": "M_UNKNOWN",
				"error":   "Bad JSON",
			})
		case "/_synapse/admin/v1/rooms/!room:d/make_room_admin":
			makeAdminCalled = true
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("{}"))
		default:
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()

	ops := NewSynapseMatrixOps(Config{
		ServerURL: server.URL, Domain: "d", AdminUser: "admin", AdminPassword: "pw",
	}, server.Client())

	err := ops.SetRoomMetadata(context.Background(), "!room:d", map[string]interface{}{"roomKind": "worker_room"}, MemberSpec{})
	if err == nil {
		t.Fatal("SetRoomMetadata: expected error for M_UNKNOWN, got nil")
	}
	if makeAdminCalled {
		t.Error("make_room_admin must not be called for non-fallback errors")
	}
}

// TestSynapseOps_ListRoomMembers_WithActorToken verifies ListRoomMembers reads
// with member.ActorToken when set (no admin login involved).
func TestSynapseOps_ListRoomMembers_WithActorToken(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/_matrix/client/v3/rooms/!room:d/members":
			if auth := r.Header.Get("Authorization"); auth != "Bearer user-token" {
				t.Errorf("Authorization = %q, want Bearer user-token", auth)
			}
			w.WriteHeader(http.StatusOK)
			json.NewEncoder(w).Encode(map[string]interface{}{
				"chunk": []map[string]interface{}{
					{"state_key": "@alice:d", "content": map[string]string{"membership": "join"}},
					{"state_key": "@bob:d", "content": map[string]string{"membership": "invite"}},
				},
			})
		default:
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()

	ops := NewSynapseMatrixOps(Config{
		ServerURL: server.URL, Domain: "d", AdminUser: "admin", AdminPassword: "pw",
	}, server.Client())

	members, err := ops.ListRoomMembers(context.Background(), "!room:d", MemberSpec{ActorToken: "user-token"})
	if err != nil {
		t.Fatalf("ListRoomMembers: %v", err)
	}
	if len(members) != 2 || members[0].UserID != "@alice:d" || members[1].UserID != "@bob:d" {
		t.Errorf("members = %+v, want [@alice:d @bob:d]", members)
	}
}

// TestSynapseOps_IsUserInRoomAndManagerJoinedDM verifies the read-only
// membership queries against a members listing (admin identity).
func TestSynapseOps_IsUserInRoomAndManagerJoinedDM(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/_matrix/client/v3/login":
			adminLoginHandler(t, w)
		case "/_matrix/client/v3/rooms/!room:d/members":
			w.WriteHeader(http.StatusOK)
			json.NewEncoder(w).Encode(map[string]interface{}{
				"chunk": []map[string]interface{}{
					{"state_key": "@alice:d", "content": map[string]string{"membership": "join"}},
					{"state_key": "@bob:d", "content": map[string]string{"membership": "invite"}},
					{"state_key": "@manager:d", "content": map[string]string{"membership": "join"}},
				},
			})
		default:
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()

	ops := NewSynapseMatrixOps(Config{
		ServerURL: server.URL, Domain: "d", AdminUser: "admin", AdminPassword: "pw",
	}, server.Client())

	ctx := context.Background()
	if in, err := ops.IsUserInRoom(ctx, "!room:d", "@alice:d"); err != nil || !in {
		t.Errorf("IsUserInRoom(@alice:d) = %v, %v; want true, nil", in, err)
	}
	if in, err := ops.IsUserInRoom(ctx, "!room:d", "@bob:d"); err != nil || in {
		t.Errorf("IsUserInRoom(@bob:d invite) = %v, %v; want false, nil", in, err)
	}
	if in, err := ops.IsManagerJoinedDM(ctx, "!room:d"); err != nil || !in {
		t.Errorf("IsManagerJoinedDM = %v, %v; want true, nil", in, err)
	}
}

// TestSynapseOps_ListJoinedRooms verifies ListJoinedRooms with the admin
// identity.
func TestSynapseOps_ListJoinedRooms(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/_matrix/client/v3/login":
			adminLoginHandler(t, w)
		case "/_matrix/client/v3/joined_rooms":
			if auth := r.Header.Get("Authorization"); auth != "Bearer admin-token" {
				t.Errorf("Authorization = %q, want Bearer admin-token", auth)
			}
			w.WriteHeader(http.StatusOK)
			json.NewEncoder(w).Encode(map[string]interface{}{
				"joined_rooms": []string{"!a:d", "!b:d"},
			})
		default:
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()

	ops := NewSynapseMatrixOps(Config{
		ServerURL: server.URL, Domain: "d", AdminUser: "admin", AdminPassword: "pw",
	}, server.Client())

	rooms, err := ops.ListJoinedRooms(context.Background(), MemberSpec{})
	if err != nil {
		t.Fatalf("ListJoinedRooms: %v", err)
	}
	if len(rooms) != 2 || rooms[0] != "!a:d" || rooms[1] != "!b:d" {
		t.Errorf("rooms = %v, want [!a:d !b:d]", rooms)
	}
}

// TestSynapseOps_HealthCheck_ServerUp verifies HealthCheck returns nil when
// the homeserver responds at the HTTP level (401 from the probe login means
// up).
func TestSynapseOps_HealthCheck_ServerUp(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/_matrix/client/v3/login":
			w.WriteHeader(http.StatusUnauthorized)
			json.NewEncoder(w).Encode(map[string]string{
				"errcode": "M_FORBIDDEN",
				"error":   "Invalid password",
			})
		default:
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()

	ops := NewSynapseMatrixOps(Config{
		ServerURL: server.URL, Domain: "d", AdminUser: "admin", AdminPassword: "pw",
	}, server.Client())

	if err := ops.HealthCheck(context.Background()); err != nil {
		t.Errorf("HealthCheck = %v, want nil for HTTP-level 401", err)
	}
}

// TestSynapseOps_HealthCheck_ServerDown verifies HealthCheck returns a
// transport error when the homeserver is unreachable.
func TestSynapseOps_HealthCheck_ServerDown(t *testing.T) {
	ops := NewSynapseMatrixOps(Config{
		// Port 1 is reserved and refused on essentially every host; no
		// listener, so the client sees "connection refused".
		ServerURL: "http://127.0.0.1:1", Domain: "d", AdminUser: "admin", AdminPassword: "pw",
	}, &http.Client{})

	if err := ops.HealthCheck(context.Background()); err == nil {
		t.Fatal("HealthCheck = nil, want transport error for unreachable server")
	}
}

// TestTuwunelOps_HealthCheck_ServerUp mirrors the Synapse up-case through the
// Tuwunel implementation.
func TestTuwunelOps_HealthCheck_ServerUp(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/_matrix/client/v3/login":
			w.WriteHeader(http.StatusUnauthorized)
			json.NewEncoder(w).Encode(map[string]string{
				"errcode": "M_FORBIDDEN",
				"error":   "Invalid password",
			})
		default:
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()

	ops := NewTuwunelMatrixOps(Config{
		ServerURL: server.URL, Domain: "d", AdminUser: "admin", AdminPassword: "pw",
	}, server.Client())

	if err := ops.HealthCheck(context.Background()); err != nil {
		t.Errorf("HealthCheck = %v, want nil for HTTP-level 401", err)
	}
}

// === Phase 4: User Identity & Credentials + AppService Governance ===

// TestSynapseOps_ProvisionUser_UsesAdminUsersEndpoint verifies ProvisionUser
// goes through the Synapse admin REST users endpoint (PUT
// /_synapse/admin/v2/users/{id} with password + displayname) followed by a
// password login for the access token. The admin API has no
// register-vs-login distinction, so Created is always reported true.
func TestSynapseOps_ProvisionUser_UsesAdminUsersEndpoint(t *testing.T) {
	var putBody map[string]interface{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/_matrix/client/v3/login":
			adminLoginHandler(t, w)
		case "/_synapse/admin/v2/users/@alice:d":
			if r.Method != http.MethodPut {
				t.Errorf("method = %s, want PUT", r.Method)
			}
			if err := json.NewDecoder(r.Body).Decode(&putBody); err != nil {
				t.Fatalf("decode users body: %v", err)
			}
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("{}"))
		default:
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()

	ops := NewSynapseMatrixOps(Config{
		ServerURL: server.URL, Domain: "d", AdminUser: "admin", AdminPassword: "pw",
	}, server.Client())

	ref, uc, err := ops.ProvisionUser(context.Background(), UserSpec{Username: "alice", Password: "pw"})
	if err != nil {
		t.Fatalf("ProvisionUser: %v", err)
	}
	if putBody["password"] != "pw" {
		t.Errorf("users body password = %v, want pw", putBody["password"])
	}
	if putBody["displayname"] != "alice" {
		t.Errorf("users body displayname = %v, want alice", putBody["displayname"])
	}
	if ref.UserID != "@alice:d" || !ref.Created {
		t.Errorf("UserRef = %+v, want UserID=@alice:d Created=true", ref)
	}
	if uc.Password != "pw" || uc.AccessToken == "" {
		t.Errorf("UserCredentials = %+v, want Password=pw + non-empty token", uc)
	}
}

// TestSynapseOps_ResetUserPassword_UsesAdminReset verifies ResetUserPassword
// maps to POST /_synapse/admin/v1/reset_password/{userId}.
func TestSynapseOps_ResetUserPassword_UsesAdminReset(t *testing.T) {
	var resetBody map[string]string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/_matrix/client/v3/login":
			adminLoginHandler(t, w)
		case "/_synapse/admin/v1/reset_password/@alice:d":
			if r.Method != http.MethodPost {
				t.Errorf("method = %s, want POST", r.Method)
			}
			if err := json.NewDecoder(r.Body).Decode(&resetBody); err != nil {
				t.Fatalf("decode reset body: %v", err)
			}
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("{}"))
		default:
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()

	ops := NewSynapseMatrixOps(Config{
		ServerURL: server.URL, Domain: "d", AdminUser: "admin", AdminPassword: "pw",
	}, server.Client())

	if err := ops.ResetUserPassword(context.Background(), "@alice:d", "newpw"); err != nil {
		t.Fatalf("ResetUserPassword: %v", err)
	}
	if resetBody["new_password"] != "newpw" {
		t.Errorf("reset body = %v, want new_password=newpw", resetBody)
	}
}

// TestSynapseOps_DeactivateUser_UsesAdminDeactivate verifies DeactivateUser
// maps to POST /_synapse/admin/v1/deactivate/{userId} with erase=false.
func TestSynapseOps_DeactivateUser_UsesAdminDeactivate(t *testing.T) {
	var deactBody map[string]bool
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/_matrix/client/v3/login":
			adminLoginHandler(t, w)
		case "/_synapse/admin/v1/deactivate/@alice:d":
			if r.Method != http.MethodPost {
				t.Errorf("method = %s, want POST", r.Method)
			}
			if err := json.NewDecoder(r.Body).Decode(&deactBody); err != nil {
				t.Fatalf("decode deactivate body: %v", err)
			}
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("{}"))
		default:
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()

	ops := NewSynapseMatrixOps(Config{
		ServerURL: server.URL, Domain: "d", AdminUser: "admin", AdminPassword: "pw",
	}, server.Client())

	if err := ops.DeactivateUser(context.Background(), "@alice:d"); err != nil {
		t.Fatalf("DeactivateUser: %v", err)
	}
	if deactBody["erase"] {
		t.Errorf("deactivate body = %v, want erase=false (data preserved)", deactBody)
	}
}

// TestSynapseOps_SetUserDisplayName_EmptyToken_UsesAdmin verifies the
// admin-identity fallback: with an empty access token, SetUserDisplayName
// routes through the admin REST users endpoint (displayname field only — the
// password is untouched).
func TestSynapseOps_SetUserDisplayName_EmptyToken_UsesAdmin(t *testing.T) {
	var putBody map[string]string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/_matrix/client/v3/login":
			adminLoginHandler(t, w)
		case "/_synapse/admin/v2/users/@alice:d":
			if r.Method != http.MethodPut {
				t.Errorf("method = %s, want PUT", r.Method)
			}
			if err := json.NewDecoder(r.Body).Decode(&putBody); err != nil {
				t.Fatalf("decode users body: %v", err)
			}
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("{}"))
		default:
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()

	ops := NewSynapseMatrixOps(Config{
		ServerURL: server.URL, Domain: "d", AdminUser: "admin", AdminPassword: "pw",
	}, server.Client())

	if err := ops.SetUserDisplayName(context.Background(), "@alice:d", "", "Alice"); err != nil {
		t.Fatalf("SetUserDisplayName (admin fallback): %v", err)
	}
	if putBody["displayname"] != "Alice" {
		t.Errorf("users body = %v, want displayname=Alice", putBody)
	}
	if _, hasPw := putBody["password"]; hasPw {
		t.Errorf("users body must not touch password: %v", putBody)
	}
}

// TestSynapseOps_SetUserDisplayName_WithToken_UsesCS verifies the user-token
// path uses the CS profile endpoint with the supplied token (no admin login).
func TestSynapseOps_SetUserDisplayName_WithToken_UsesCS(t *testing.T) {
	var putBody map[string]string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/_matrix/client/v3/profile/@alice:d/displayname":
			if r.Method != http.MethodPut {
				t.Errorf("method = %s, want PUT", r.Method)
			}
			if auth := r.Header.Get("Authorization"); auth != "Bearer user-token" {
				t.Errorf("Authorization = %q, want Bearer user-token", auth)
			}
			if err := json.NewDecoder(r.Body).Decode(&putBody); err != nil {
				t.Fatalf("decode profile body: %v", err)
			}
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("{}"))
		default:
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()

	ops := NewSynapseMatrixOps(Config{
		ServerURL: server.URL, Domain: "d", AdminUser: "admin", AdminPassword: "pw",
	}, server.Client())

	if err := ops.SetUserDisplayName(context.Background(), "@alice:d", "user-token", "Alice"); err != nil {
		t.Fatalf("SetUserDisplayName (with token): %v", err)
	}
	if putBody["displayname"] != "Alice" {
		t.Errorf("profile body = %v, want displayname=Alice", putBody)
	}
}

// TestSynapseOps_RegisterAppService_SmokeTestOnly_Success verifies that on
// Synapse RegisterAppService does NOT issue any admin command — it only runs
// the AS smoke test (an AS login as the sender_localpart), succeeding when the
// declarative registration is already active.
func TestSynapseOps_RegisterAppService_SmokeTestOnly_Success(t *testing.T) {
	var asLogins int
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/_matrix/client/v3/login":
			asLogins++
			if auth := r.Header.Get("Authorization"); auth != "Bearer as-token" {
				t.Errorf("Authorization = %q, want Bearer as-token", auth)
			}
			w.WriteHeader(http.StatusOK)
			json.NewEncoder(w).Encode(map[string]string{"access_token": "t"})
		default:
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()

	ops := NewSynapseMatrixOps(Config{
		ServerURL: server.URL, Domain: "d", AdminUser: "admin", AdminPassword: "pw",
		AppServiceSenderLocalpart: "agentteams-controller",
		AppServiceToken:           "as-token",
	}, server.Client())

	err := ops.RegisterAppService(context.Background(), AppServiceRegistration{ID: "agentteams-controller"})
	if err != nil {
		t.Fatalf("RegisterAppService: %v", err)
	}
	if asLogins != 1 {
		t.Errorf("AS logins = %d, want 1 (smoke test only, no admin command)", asLogins)
	}
}

// TestSynapseOps_RegisterAppService_ErrorWhenNotActive verifies that when the
// declarative registration is not active, RegisterAppService reports an error
// pointing the operator at the Helm-managed configuration instead of trying to
// register at runtime. A short-lived context aborts the smoke-test retry loop.
func TestSynapseOps_RegisterAppService_ErrorWhenNotActive(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/_matrix/client/v3/login":
			w.WriteHeader(http.StatusUnauthorized)
			json.NewEncoder(w).Encode(map[string]string{
				"errcode": "M_UNKNOWN_TOKEN",
				"error":   "Appservice not registered",
			})
		default:
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()

	ops := NewSynapseMatrixOps(Config{
		ServerURL: server.URL, Domain: "d", AdminUser: "admin", AdminPassword: "pw",
		AppServiceSenderLocalpart: "agentteams-controller",
		AppServiceToken:           "as-token",
	}, server.Client())

	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()

	err := ops.RegisterAppService(ctx, AppServiceRegistration{ID: "agentteams-controller"})
	if err == nil {
		t.Fatal("RegisterAppService = nil, want error for inactive declarative appservice")
	}
	if !strings.Contains(err.Error(), "declarative") || !strings.Contains(err.Error(), "Helm") {
		t.Errorf("error = %q, want Helm/declarative guidance", err)
	}
}

// TestSynapseOps_UnregisterAppService_ErrorPointsToHelm verifies
// UnregisterAppService returns an error pointing at Helm (declarative
// registrations cannot be removed at runtime) without issuing any HTTP call.
func TestSynapseOps_UnregisterAppService_ErrorPointsToHelm(t *testing.T) {
	ops := NewSynapseMatrixOps(Config{
		ServerURL: "http://127.0.0.1:1", Domain: "d", AdminUser: "admin", AdminPassword: "pw",
	}, &http.Client{})

	err := ops.UnregisterAppService(context.Background(), "agentteams-controller")
	if err == nil {
		t.Fatal("UnregisterAppService = nil, want error pointing at Helm")
	}
	if !strings.Contains(err.Error(), "Helm") || !strings.Contains(err.Error(), "declarative") {
		t.Errorf("error = %q, want Helm/declarative guidance", err)
	}
}
