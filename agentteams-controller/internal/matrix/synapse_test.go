package matrix

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// TestNewClientProviderSelection pins the provider → implementation mapping
// used by the app wiring and the AppService mgmt handler.
func TestNewClientProviderSelection(t *testing.T) {
	cfg := Config{ServerURL: "http://matrix.invalid", Domain: "d"}

	for _, provider := range []string{"", "tuwunel", "Tuwunel", "  TUWUNEL  "} {
		client, err := NewClient(provider, cfg, nil)
		if err != nil {
			t.Fatalf("NewClient(%q): unexpected error: %v", provider, err)
		}
		if _, ok := client.(*TuwunelClient); !ok {
			t.Fatalf("NewClient(%q) = %T, want *TuwunelClient", provider, client)
		}
	}

	client, err := NewClient("synapse", cfg, nil)
	if err != nil {
		t.Fatalf("NewClient(synapse): unexpected error: %v", err)
	}
	if _, ok := client.(*SynapseClient); !ok {
		t.Fatalf("NewClient(synapse) = %T, want *SynapseClient", client)
	}

	if _, err := NewClient("tuwunel2", cfg, nil); err == nil {
		t.Fatalf("NewClient(tuwunel2) = nil error, want unknown-provider error")
	}
}

// TestSynapseEnsureUserAdminAPI verifies that Synapse user provisioning goes
// through the admin users API (not the Tuwunel registration_token flow) and
// that the returned credentials are usable.
func TestSynapseEnsureUserAdminAPI(t *testing.T) {
	var putPath string
	var putBody map[string]interface{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodPut && strings.HasPrefix(r.URL.Path, "/_synapse/admin/v2/users/"):
			putPath = r.URL.Path
			_ = json.NewDecoder(r.Body).Decode(&putBody)
			w.Header().Set("Content-Type", "application/json")
			_, _ = io.WriteString(w, `{}`)
		case r.Method == http.MethodPost && r.URL.Path == "/_matrix/client/v3/login":
			w.Header().Set("Content-Type", "application/json")
			_, _ = io.WriteString(w, `{"access_token":"synapse-token","user_id":"@worker:d"}`)
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()

	client := NewSynapseClient(Config{ServerURL: srv.URL, Domain: "d", AdminUser: "admin", AdminPassword: "admin-pw"}, srv.Client())
	creds, err := client.EnsureUser(context.Background(), EnsureUserRequest{Username: "worker", Password: "secret-pw"})
	if err != nil {
		t.Fatalf("EnsureUser: %v", err)
	}
	if putPath != "/_synapse/admin/v2/users/"+"@worker:d" && putPath != "/_synapse/admin/v2/users/%40worker%3Ad" {
		t.Fatalf("admin API path = %q, want the v2 users endpoint for @worker:d", putPath)
	}
	if putBody["password"] != "secret-pw" {
		t.Fatalf("admin API body password = %v, want secret-pw", putBody["password"])
	}
	if putBody["logout_devices"] != false {
		t.Fatalf("admin API body logout_devices = %v, want false (keep worker sessions alive)", putBody["logout_devices"])
	}
	if creds.UserID != "@worker:d" || creds.AccessToken != "synapse-token" || creds.Password != "secret-pw" || !creds.Created {
		t.Fatalf("EnsureUser creds = %+v, want @worker:d / synapse-token / Created", creds)
	}
}

// TestSynapseCreateRoomCreatorFixes verifies Synapse's stricter createRoom
// contract: the creator is pinned in power_level_content_override and stripped
// from the invite list.
func TestSynapseCreateRoomCreatorFixes(t *testing.T) {
	var createBody map[string]interface{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/_matrix/client/v3/login":
			w.Header().Set("Content-Type", "application/json")
			_, _ = io.WriteString(w, `{"access_token":"admin-token"}`)
		case r.Method == http.MethodPost && r.URL.Path == "/_matrix/client/v3/createRoom":
			_ = json.NewDecoder(r.Body).Decode(&createBody)
			w.Header().Set("Content-Type", "application/json")
			_, _ = io.WriteString(w, `{"room_id":"!room:d"}`)
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()

	client := NewSynapseClient(Config{
		ServerURL: srv.URL, Domain: "d", AdminUser: "admin", AdminPassword: "admin-pw",
	}, srv.Client())

	_, err := client.CreateRoom(context.Background(), CreateRoomRequest{
		Name:          "Team: test",
		RoomAliasName: "agentteams-team-test",
		Invite:        []string{"@admin:d", "@worker:d"},
		PowerLevels:   map[string]int{"@worker:d": 50},
	})
	if err != nil {
		t.Fatalf("CreateRoom: %v", err)
	}
	if createBody == nil {
		t.Fatalf("createRoom was not called")
	}
	invite, _ := createBody["invite"].([]interface{})
	for _, raw := range invite {
		if raw == "@admin:d" {
			t.Fatalf("creator @admin:d must be stripped from invite list, got %v", invite)
		}
	}
	override, _ := createBody["power_level_content_override"].(map[string]interface{})
	users, _ := override["users"].(map[string]interface{})
	if users["@admin:d"] != float64(100) {
		t.Fatalf("creator power level = %v, want 100 (body=%v)", users["@admin:d"], override)
	}
	if users["@worker:d"] != float64(50) {
		t.Fatalf("worker power level = %v, want 50", users["@worker:d"])
	}
}

// TestSynapseAdminCommandUnsupported guards the "no admin bot on Synapse"
// contract: business code must use ProviderAdminOps instead.
func TestSynapseAdminCommandUnsupported(t *testing.T) {
	client := NewSynapseClient(Config{ServerURL: "http://matrix.invalid", Domain: "d"}, nil)
	if err := client.AdminCommand(context.Background(), "!admin rooms delete-room !r:d"); err == nil {
		t.Fatalf("AdminCommand on Synapse = nil, want explicit unsupported error")
	}
}

// TestSynapseDeclarativeAppService covers the file-based registration contract:
// RegisterAppService only verifies the active registration (smoke test) and
// UnregisterAppService refuses to mutate runtime state.
func TestSynapseDeclarativeAppService(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost && r.URL.Path == "/_matrix/client/v3/login" {
			w.Header().Set("Content-Type", "application/json")
			_, _ = io.WriteString(w, `{"access_token":"as-token","user_id":"@agentteams-controller:d"}`)
			return
		}
		http.NotFound(w, r)
	}))
	defer srv.Close()

	client := NewSynapseClient(Config{
		ServerURL:                 srv.URL,
		Domain:                    "d",
		AppServiceToken:           "as-secret",
		AppServiceSenderLocalpart: "agentteams-controller",
	}, srv.Client())

	if err := client.RegisterAppService(context.Background(), AppServiceRegistration{ID: "agentteams-controller"}); err != nil {
		t.Fatalf("RegisterAppService (active registration): %v", err)
	}
	if err := client.UnregisterAppService(context.Background(), "agentteams-controller"); err == nil {
		t.Fatalf("UnregisterAppService on Synapse = nil, want declarative-registration error")
	}
}

// TestClassifySynapseSenderError pins the failure classification that drives
// the join / make_room_admin recovery.
func TestClassifySynapseSenderError(t *testing.T) {
	tests := []struct {
		name string
		err  error
		want synapseSenderRecovery
	}{
		{"nil", nil, synapseRecoveryNone},
		{"not-joined", fmt.Errorf(`kick: HTTP 403 M_FORBIDDEN: @admin:d not in room !r:d`), synapseRecoveryJoin},
		{"kick-power", fmt.Errorf(`kick: HTTP 403 M_FORBIDDEN: You cannot kick user @manager:d.`), synapseRecoveryPower},
		{"invite-power", fmt.Errorf(`invite: HTTP 403 M_FORBIDDEN: You don't have permission to invite users`), synapseRecoveryPower},
		{"target-not-in-room", fmt.Errorf(`kick: HTTP 403 M_FORBIDDEN: The target user is not in the room`), synapseRecoveryNone},
		{"other", fmt.Errorf(`create room: HTTP 429 M_LIMIT_EXCEEDED`), synapseRecoveryNone},
	}
	for _, tc := range tests {
		if got := classifySynapseSenderError(tc.err); got != tc.want {
			t.Errorf("%s: classifySynapseSenderError = %v, want %v", tc.name, got, tc.want)
		}
	}
}

// TestNormalizeJSONNumbers verifies power-level state read back from Synapse is
// normalized before being written again (Synapse validates integers).
func TestNormalizeJSONNumbers(t *testing.T) {
	in := map[string]interface{}{
		"users": map[string]interface{}{
			"@admin:d":  float64(100),
			"@worker:d": float64(0),
		},
		"state_default": float64(50),
		"events":        map[string]interface{}{"m.room.name": float64(50)},
		"list":          []interface{}{float64(1), "x"},
		"fraction":      1.5,
	}
	out := normalizeJSONNumbers(in).(map[string]interface{})
	users := out["users"].(map[string]interface{})
	if users["@admin:d"] != int64(100) {
		t.Errorf("users[@admin:d] = %T %v, want int64(100)", users["@admin:d"], users["@admin:d"])
	}
	if out["state_default"] != int64(50) {
		t.Errorf("state_default = %T %v, want int64(50)", out["state_default"], out["state_default"])
	}
	if out["fraction"] != 1.5 {
		t.Errorf("fraction = %v, want 1.5 (non-integral floats preserved)", out["fraction"])
	}
	list := out["list"].([]interface{})
	if list[0] != int64(1) || list[1] != "x" {
		t.Errorf("list = %v, want [int64(1) x]", list)
	}
}
