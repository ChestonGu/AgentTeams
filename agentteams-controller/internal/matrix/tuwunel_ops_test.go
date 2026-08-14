package matrix

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// These tests exercise TuwunelMatrixOps through a mocked Tuwunel homeserver
// (httptest). They assert the same CS API calls and admin-bot command strings
// that the pre-abstraction service layer produced, so they double as the
// zero-regression contract for Tuwunel behavior.

func TestTuwunelOps_CreateRoom(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/_matrix/client/v3/login":
			adminLoginHandler(t, w)
		case "/_matrix/client/v3/createRoom":
			if r.Method != http.MethodPost {
				t.Errorf("createRoom method = %s, want POST", r.Method)
			}
			if auth := r.Header.Get("Authorization"); auth != "Bearer admin-token" {
				t.Errorf("Authorization = %q, want Bearer admin-token", auth)
			}
			var body map[string]interface{}
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Fatalf("decode createRoom body: %v", err)
			}
			if body["name"] != "Alice Room" {
				t.Errorf("name = %v, want Alice Room", body["name"])
			}
			if body["room_alias_name"] != "alice-worker" {
				t.Errorf("room_alias_name = %v, want alice-worker", body["room_alias_name"])
			}
			if body["preset"] != "trusted_private_chat" {
				t.Errorf("preset = %v, want trusted_private_chat", body["preset"])
			}
			pl, ok := body["power_level_content_override"].(map[string]interface{})
			if !ok {
				t.Fatalf("missing power_level_content_override in %v", body)
			}
			users, ok := pl["users"].(map[string]interface{})
			if !ok {
				t.Fatalf("missing users in power_level_content_override")
			}
			if users["@alice:d"] != float64(0) || users["@admin:d"] != float64(100) {
				t.Errorf("power levels = %v, want @alice:d=0 @admin:d=100", users)
			}
			initialState, ok := body["initial_state"].([]interface{})
			if !ok || len(initialState) == 0 {
				t.Fatalf("missing initial_state")
			}
			foundMeta := false
			for _, ev := range initialState {
				em, ok := ev.(map[string]interface{})
				if ok && em["type"] == "room.meta" {
					foundMeta = true
					content := em["content"].(map[string]interface{})
					if content["roomKind"] != "worker_room" || content["schemaVersion"] != float64(1) {
						t.Errorf("room.meta content = %v", content)
					}
					if content["workerName"] != "alice" {
						t.Errorf("room.meta extra workerName = %v, want alice", content["workerName"])
					}
				}
			}
			if !foundMeta {
				t.Errorf("initial_state missing room.meta event")
			}
			w.WriteHeader(http.StatusOK)
			json.NewEncoder(w).Encode(map[string]string{"room_id": "!worker:d"})
		default:
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()

	ops := NewTuwunelMatrixOps(Config{
		ServerURL: server.URL, Domain: "d", AdminUser: "admin", AdminPassword: "pw",
	}, server.Client())

	ref, err := ops.CreateRoom(context.Background(), RoomSpec{
		Name:           "Alice Room",
		AliasLocalpart: "alice-worker",
		PowerLevels:    map[string]int{"@alice:d": 0, "@admin:d": 100},
		Metadata: &RoomMetadata{
			Kind:          "worker_room",
			SchemaVersion: 1,
			Extra:         map[string]interface{}{"workerName": "alice"},
		},
	})
	if err != nil {
		t.Fatalf("CreateRoom: %v", err)
	}
	if ref.RoomID != "!worker:d" || !ref.Created {
		t.Errorf("RoomRef = %+v, want RoomID=!worker:d Created=true", ref)
	}
}

func TestTuwunelOps_CreateRoom_ActorTokenPassthrough(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/_matrix/client/v3/createRoom":
			// ActorToken must reach the CS API as the bearer token, so the
			// room is created AS the team admin (not the global admin) —
			// byte-equivalent to the pre-abstraction service layer, which set
			// CreateRoomRequest.CreatorToken = req.TeamAdminActorToken.
			if auth := r.Header.Get("Authorization"); auth != "Bearer team-admin-token" {
				t.Errorf("Authorization = %q, want Bearer team-admin-token", auth)
			}
			w.WriteHeader(http.StatusOK)
			json.NewEncoder(w).Encode(map[string]string{"room_id": "!team:d"})
		default:
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()

	ops := NewTuwunelMatrixOps(Config{
		ServerURL: server.URL, Domain: "d", AdminUser: "admin", AdminPassword: "pw",
	}, server.Client())

	ref, err := ops.CreateRoom(context.Background(), RoomSpec{
		Name:           "Team Room",
		AliasLocalpart: "team-alpha",
		ActorUserID:    "@team-admin:d",
		ActorToken:     "team-admin-token",
	})
	if err != nil {
		t.Fatalf("CreateRoom: %v", err)
	}
	if ref.RoomID != "!team:d" || !ref.Created {
		t.Errorf("RoomRef = %+v, want RoomID=!team:d Created=true", ref)
	}
}

func TestTuwunelOps_DissolveRoom_SendsAdminBotCommand(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/_matrix/client/v3/login":
			adminLoginHandler(t, w)
		case "/_matrix/client/v3/directory/room/#admins:d":
			w.WriteHeader(http.StatusOK)
			json.NewEncoder(w).Encode(map[string]string{"room_id": "!admins:d"})
		default:
			if strings.HasPrefix(r.URL.Path, "/_matrix/client/v3/rooms/!admins:d/send/m.room.message/") {
				var msg map[string]string
				if err := json.NewDecoder(r.Body).Decode(&msg); err != nil {
					t.Fatalf("decode admin message: %v", err)
				}
				if msg["body"] != "!admin rooms delete-room !room:d" {
					t.Errorf("admin bot body = %q, want !admin rooms delete-room !room:d", msg["body"])
				}
				w.WriteHeader(http.StatusOK)
				w.Write([]byte("{}"))
				return
			}
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()

	ops := NewTuwunelMatrixOps(Config{
		ServerURL: server.URL, Domain: "d", AdminUser: "admin", AdminPassword: "pw",
	}, server.Client())

	if err := ops.DissolveRoom(context.Background(), "!room:d"); err != nil {
		t.Fatalf("DissolveRoom: %v", err)
	}
}

func TestTuwunelOps_AddMember(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/_matrix/client/v3/login":
			adminLoginHandler(t, w)
		case "/_matrix/client/v3/rooms/!room:d/invite":
			var body map[string]string
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Fatalf("decode invite body: %v", err)
			}
			if body["user_id"] != "@alice:d" {
				t.Errorf("invite user_id = %q, want @alice:d", body["user_id"])
			}
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("{}"))
		default:
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()

	ops := NewTuwunelMatrixOps(Config{
		ServerURL: server.URL, Domain: "d", AdminUser: "admin", AdminPassword: "pw",
	}, server.Client())

	if err := ops.AddMember(context.Background(), "!room:d", "@alice:d"); err != nil {
		t.Fatalf("AddMember: %v", err)
	}
}

func TestTuwunelOps_AddMember_AlreadyInRoom(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/_matrix/client/v3/login":
			adminLoginHandler(t, w)
		case "/_matrix/client/v3/rooms/!room:d/invite":
			w.WriteHeader(http.StatusForbidden)
			json.NewEncoder(w).Encode(map[string]string{
				"errcode": "M_FORBIDDEN",
				"error":   "@alice:d is already in the room.",
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

	if err := ops.AddMember(context.Background(), "!room:d", "@alice:d"); err != nil {
		t.Errorf("expected nil for already-in-room, got %v", err)
	}
}

func TestTuwunelOps_RemoveMember(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/_matrix/client/v3/login":
			adminLoginHandler(t, w)
		case "/_matrix/client/v3/rooms/!room:d/kick":
			var body map[string]string
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Fatalf("decode kick body: %v", err)
			}
			if body["user_id"] != "@alice:d" {
				t.Errorf("kick user_id = %q, want @alice:d", body["user_id"])
			}
			if body["reason"] != "access revoked" {
				t.Errorf("kick reason = %q, want access revoked", body["reason"])
			}
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("{}"))
		default:
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()

	ops := NewTuwunelMatrixOps(Config{
		ServerURL: server.URL, Domain: "d", AdminUser: "admin", AdminPassword: "pw",
	}, server.Client())

	if err := ops.RemoveMember(context.Background(), "!room:d", "@alice:d", "access revoked"); err != nil {
		t.Fatalf("RemoveMember: %v", err)
	}
}

func TestTuwunelOps_RemoveMember_TargetNotInRoom(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/_matrix/client/v3/login":
			adminLoginHandler(t, w)
		case "/_matrix/client/v3/rooms/!room:d/kick":
			w.WriteHeader(http.StatusForbidden)
			json.NewEncoder(w).Encode(map[string]string{
				"errcode": "M_FORBIDDEN",
				"error":   "User @alice:d is not in the room.",
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

	if err := ops.RemoveMember(context.Background(), "!room:d", "@alice:d", ""); err != nil {
		t.Errorf("expected nil for target-not-in-room, got %v", err)
	}
}

func TestTuwunelOps_RemoveMember_FallbackForceLeave(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/_matrix/client/v3/login":
			adminLoginHandler(t, w)
		case "/_matrix/client/v3/rooms/!room:d/kick":
			w.WriteHeader(http.StatusForbidden)
			json.NewEncoder(w).Encode(map[string]string{
				"errcode": "M_FORBIDDEN",
				"error":   "sender does not have enough power to kick target user",
			})
		case "/_matrix/client/v3/directory/room/#admins:d":
			w.WriteHeader(http.StatusOK)
			json.NewEncoder(w).Encode(map[string]string{"room_id": "!admins:d"})
		default:
			if strings.HasPrefix(r.URL.Path, "/_matrix/client/v3/rooms/!admins:d/send/m.room.message/") {
				var msg map[string]string
				if err := json.NewDecoder(r.Body).Decode(&msg); err != nil {
					t.Fatalf("decode admin message: %v", err)
				}
				if msg["body"] != "!admin users force-leave-room @alice:d !room:d" {
					t.Errorf("admin bot body = %q, want !admin users force-leave-room @alice:d !room:d", msg["body"])
				}
				w.WriteHeader(http.StatusOK)
				w.Write([]byte("{}"))
				return
			}
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()

	ops := NewTuwunelMatrixOps(Config{
		ServerURL: server.URL, Domain: "d", AdminUser: "admin", AdminPassword: "pw",
	}, server.Client())

	if err := ops.RemoveMember(context.Background(), "!room:d", "@alice:d", ""); err != nil {
		t.Fatalf("RemoveMember with force-leave fallback: %v", err)
	}
}
