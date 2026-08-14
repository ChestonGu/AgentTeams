package server

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/agentscope-ai/AgentTeams/agentteams-controller/internal/matrix"
)

// --- AppService token rotation (POST /api/v1/appservice/rotate-token) ---

// TestAppServiceHandler_RotateToken_Synapse_501 verifies that RotateToken
// answers 501 with Helm guidance when the provider is synapse — Synapse
// AppService registrations are declarative (Helm-managed) and cannot be
// rotated at runtime.
func TestAppServiceHandler_RotateToken_Synapse_501(t *testing.T) {
	h := NewAppServiceHandler(matrix.Config{Provider: "synapse"})

	req := httptest.NewRequest(http.MethodPost, "/api/v1/appservice/rotate-token",
		strings.NewReader(`{"as_token":"new-token"}`))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.RotateToken(rec, req)

	if rec.Code != http.StatusNotImplemented {
		t.Fatalf("status = %d, want 501; body=%q", rec.Code, rec.Body.String())
	}
	body := rec.Body.String()
	if !strings.Contains(body, "declarative") || !strings.Contains(body, "Helm") {
		t.Errorf("body = %q, want Helm/declarative guidance", body)
	}
}

// TestAppServiceHandler_RotateToken_Tuwunel_Success verifies that on the
// default/tuwunel provider RotateToken routes through the provider factory
// (matrix.NewOps) instead of a hardcoded NewTuwunelClient: the new as_token is
// used for the AppService smoke test (AS login as the sender_localpart), and a
// successful rotation returns 200.
func TestAppServiceHandler_RotateToken_Tuwunel_Success(t *testing.T) {
	var asLogins int
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/_matrix/client/v3/login":
			asLogins++
			if auth := r.Header.Get("Authorization"); auth != "Bearer new-token" {
				t.Errorf("Authorization = %q, want Bearer new-token (rotated as_token)", auth)
			}
			w.WriteHeader(http.StatusOK)
			json.NewEncoder(w).Encode(map[string]string{"access_token": "t"})
		default:
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()

	h := NewAppServiceHandler(matrix.Config{
		Provider:                  "tuwunel",
		ServerURL:                 server.URL,
		Domain:                    "d",
		AppServiceID:              "agentteams-controller",
		AppServiceToken:           "old-token",
		AppServiceHSToken:         "hs-token",
		AppServiceSenderLocalpart: "agentteams-controller",
	})

	req := httptest.NewRequest(http.MethodPost, "/api/v1/appservice/rotate-token",
		strings.NewReader(`{"as_token":"new-token"}`))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()

	h.RotateToken(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%q", rec.Code, rec.Body.String())
	}
	if asLogins != 2 {
		t.Errorf("AS logins = %d, want 2 (register smoke-test fast path + explicit smoke test)", asLogins)
	}
}
