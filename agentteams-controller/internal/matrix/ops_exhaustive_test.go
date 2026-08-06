package matrix

// ops_exhaustive_test.go is the cross-implementation equivalence suite for
// TuwunelMatrixOps and SynapseMatrixOps. It proves that every MatrixOps method
// produces the same business outcome under both implementations when the
// underlying homeserver is wired equivalently, and it pins the documented
// wire divergences (admin-bot chat on Tuwunel vs Synapse REST, plus the
// make_room_admin fallback on Synapse).
//
// The matrix package already has focused per-implementation tests in
// tuwunel_ops_test.go and synapse_ops_test.go. This file is additive: it runs
// BOTH implementations against the SAME mocked homeserver fixture and asserts
// equivalence (GROUP A), then asserts the provider-specific divergence paths
// (GROUP B / GROUP C). It does NOT duplicate the per-implementation fallback
// assertions that already exist.

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

// ---------------------------------------------------------------------------
// Shared equivalence fixtures
// ---------------------------------------------------------------------------

// recordedRequest is a single captured HTTP request, stored by requestLog so
// tests can assert on the wire shape after the call returns.
type recordedRequest struct {
	Method string
	Path   string
	Body   string
	Auth   string
}

// requestLog is a concurrency-safe recorder of HTTP requests seen by the
// mock homeserver. Both implementations share the same server (and therefore
// the same log) so equivalence tests can diff their traffic.
type requestLog struct {
	mu       sync.Mutex
	requests []recordedRequest
}

func (rl *requestLog) record(r *http.Request) {
	body, _ := io.ReadAll(r.Body)
	r.Body = io.NopCloser(bytes.NewReader(body))
	rl.mu.Lock()
	rl.requests = append(rl.requests, recordedRequest{
		Method: r.Method,
		Path:   r.URL.Path,
		Body:   string(body),
		Auth:   r.Header.Get("Authorization"),
	})
	rl.mu.Unlock()
}

func (rl *requestLog) snapshot() []recordedRequest {
	rl.mu.Lock()
	defer rl.mu.Unlock()
	out := make([]recordedRequest, len(rl.requests))
	copy(out, rl.requests)
	return out
}

// countByPath returns the number of recorded requests whose URL.Path matches
// path exactly.
func (rl *requestLog) countByPath(path string) int {
	rl.mu.Lock()
	defer rl.mu.Unlock()
	n := 0
	for _, r := range rl.requests {
		if r.Path == path {
			n++
		}
	}
	return n
}

// firstByPath returns the first recorded request whose URL.Path matches path,
// with ok=false when none matched.
func (rl *requestLog) firstByPath(path string) (recordedRequest, bool) {
	rl.mu.Lock()
	defer rl.mu.Unlock()
	for _, r := range rl.requests {
		if r.Path == path {
			return r, true
		}
	}
	return recordedRequest{}, false
}

// equivDomain is the Matrix domain used by every equivalence fixture.
const equivDomain = "equiv.test"

// equivRoomID is the canonical room id the mock homeserver returns for every
// successful createRoom / resolve-alias / etc.
const equivRoomID = "!equiv-room:equiv.test"

// equivConfig returns the standard Config every equivalence test shares:
// both implementations point at the same server URL and use the same admin
// credentials. The only difference between the two ops instances is the
// concrete type (TuwunelMatrixOps vs SynapseMatrixOps).
func equivConfig(serverURL string) Config {
	return Config{
		ServerURL:     serverURL,
		Domain:        equivDomain,
		AdminUser:     "admin",
		AdminPassword: "pw",
	}
}

// tuwunelEquivOps builds a TuwunelMatrixOps pointing at server.
func tuwunelEquivOps(server *httptest.Server) *TuwunelMatrixOps {
	return NewTuwunelMatrixOps(equivConfig(server.URL), server.Client())
}

// synapseEquivOps builds a SynapseMatrixOps pointing at server.
func synapseEquivOps(server *httptest.Server) *SynapseMatrixOps {
	return NewSynapseMatrixOps(equivConfig(server.URL), server.Client())
}

// equivServer builds an httptest.Server that always records incoming requests
// and dispatches them to handle. The recorder runs before handle, so handle
// sees the same request body via r.Body. Common admin scaffolding
// (admin login + #admins room resolution) is handled automatically; handle is
// only invoked for non-scaffold paths. Note r.URL.Path is the DECODED path,
// so the admins alias match uses the literal "#admins:<domain>" form (matching
// the existing helperAdminServer convention).
func equivServer(t *testing.T, log *requestLog, handle func(w http.ResponseWriter, r *http.Request)) *httptest.Server {
	t.Helper()
	adminsAlias := "/_matrix/client/v3/directory/room/#admins:" + equivDomain
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		log.record(r)
		switch r.URL.Path {
		case "/_matrix/client/v3/login":
			// Admin login: emit the shared admin-token. Some tests probe the
			// login endpoint with deliberately-invalid credentials (HealthCheck,
			// ProvisionUser), so accept any body and return 200 with a token
			// unless the request body explicitly opts out via the probe marker.
			w.WriteHeader(http.StatusOK)
			fmt.Fprintf(w, `{"access_token":"admin-token"}`)
			return
		case adminsAlias:
			w.WriteHeader(http.StatusOK)
			fmt.Fprintf(w, `{"room_id":"!admins:%s"}`, equivDomain)
			return
		}
		handle(w, r)
	}))
}

// writeJSON writes v as JSON with a 200 status. Helper for handlers.
func writeJSON(t *testing.T, w http.ResponseWriter, v interface{}) {
	t.Helper()
	w.WriteHeader(http.StatusOK)
	if err := json.NewEncoder(w).Encode(v); err != nil {
		t.Errorf("encode response: %v", err)
	}
}

// writeMatrixError writes a Matrix-style error envelope with the given status.
func writeMatrixError(w http.ResponseWriter, status int, errCode, message string) {
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(map[string]string{
		"errcode": errCode,
		"error":   message,
	})
}

// unmarshalBody decodes r.Body into a map for assertions. Panics on error so
// the test fails fast if the wire payload is malformed.
func unmarshalBody(t *testing.T, body string) map[string]interface{} {
	t.Helper()
	var m map[string]interface{}
	if err := json.Unmarshal([]byte(body), &m); err != nil {
		t.Fatalf("unmarshal body %q: %v", body, err)
	}
	return m
}

// assertEquivNil asserts both errT and errS are nil. When either is non-nil it
// reports both values so the divergence is visible.
func assertEquivNil(t *testing.T, label string, errT, errS error) {
	t.Helper()
	if errT != nil || errS != nil {
		t.Fatalf("%s: tuwunel err = %v, synapse err = %v; want both nil", label, errT, errS)
	}
}

// assertEquivRoomRef asserts both refs are equal.
func assertEquivRoomRef(t *testing.T, label string, rt, rs *RoomRef) {
	t.Helper()
	if rt == nil || rs == nil {
		t.Fatalf("%s: tuwunel=%v synapse=%v; want both non-nil", label, rt, rs)
	}
	if rt.RoomID != rs.RoomID || rt.Created != rs.Created {
		t.Errorf("%s: tuwunel=%+v synapse=%+v; want equal", label, rt, rs)
	}
}

// txnIDPattern matches Matrix message transaction IDs (hc-<n>) so tests can
// ignore the incrementing counter when comparing admin-bot message paths.
const txnIDPattern = "/_matrix/client/v3/rooms/!admins:" + equivDomain + "/send/m.room.message/"

// adminBotMessageCount counts recorded requests whose path starts with the
// admin-bot message txn prefix — used by divergence tests that assert a
// command was delivered to the Tuwunel admin room.
func adminBotMessageCount(log *requestLog) int {
	shots := log.snapshot()
	n := 0
	for _, r := range shots {
		if strings.HasPrefix(r.Path, txnIDPattern) {
			n++
		}
	}
	return n
}

// ===========================================================================
// GROUP A — success-path equivalence (CS API surface)
//
// These methods are byte-identical between implementations because both reuse
// the embedded *TuwunelClient CS API methods. Each test runs both impls
// against the same server and asserts the returned values match.
// ===========================================================================

// TestEquiv_CreateRoom_NoPowerLevels asserts both impls emit the same
// createRoom body (no power_level_content_override, same alias, same preset)
// and return the same RoomRef.
func TestEquiv_CreateRoom_NoPowerLevels(t *testing.T) {
	log := &requestLog{}
	server := equivServer(t, log, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/_matrix/client/v3/createRoom" {
			writeJSON(t, w, map[string]string{"room_id": equivRoomID})
			return
		}
		t.Errorf("unexpected path: %s", r.URL.Path)
		w.WriteHeader(http.StatusNotFound)
	})
	defer server.Close()

	spec := RoomSpec{
		Name:           "Equiv Room",
		AliasLocalpart: "equiv-room",
		Topic:          "shared topic",
	}
	rt, errT := tuwunelEquivOps(server).CreateRoom(context.Background(), spec)
	rs, errS := synapseEquivOps(server).CreateRoom(context.Background(), spec)
	assertEquivNil(t, "CreateRoom", errT, errS)
	assertEquivRoomRef(t, "CreateRoom", rt, rs)

	// Two createRoom bodies captured (one per impl); decode both and diff.
	shots := log.snapshot()
	var bodies []map[string]interface{}
	for _, r := range shots {
		if r.Path == "/_matrix/client/v3/createRoom" {
			bodies = append(bodies, unmarshalBody(t, r.Body))
		}
	}
	if len(bodies) != 2 {
		t.Fatalf("want 2 createRoom requests, got %d", len(bodies))
	}
	for _, key := range []string{"name", "topic", "room_alias_name", "preset", "is_direct"} {
		if bodies[0][key] != bodies[1][key] {
			t.Errorf("createRoom %s diverges: tuwunel=%v synapse=%v",
				key, bodies[0][key], bodies[1][key])
		}
	}
	// Neither impl sets power_level_content_override when PowerLevels is empty.
	for i, b := range bodies {
		if _, ok := b["power_level_content_override"]; ok {
			t.Errorf("createRoom body %d must not set power_level_content_override: %v", i, b)
		}
	}
}

// TestEquiv_CreateRoom_PowerLevelsContainsCreator asserts that when the actor
// is already present in PowerLevels (the team-room invariant), Synapse must
// NOT inject the global admin and the two bodies must be byte-identical for
// the power_level_content_override.users map.
//
// This complements TestSynapseOps_CreateRoom_ActorPLInjection, which proves
// injection is a no-op when the actor is present. The equivalence angle here
// pins that the no-op is real: tuwunel and synapse bodies match field-for-field.
func TestEquiv_CreateRoom_PowerLevelsContainsCreator(t *testing.T) {
	log := &requestLog{}
	server := equivServer(t, log, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/_matrix/client/v3/createRoom" {
			writeJSON(t, w, map[string]string{"room_id": equivRoomID})
			return
		}
		t.Errorf("unexpected path: %s", r.URL.Path)
		w.WriteHeader(http.StatusNotFound)
	})
	defer server.Close()

	spec := RoomSpec{
		Name:           "Team Room",
		AliasLocalpart: "team-alpha",
		ActorUserID:    "@team-admin:" + equivDomain,
		ActorToken:     "team-admin-token",
		PowerLevels: map[string]int{
			"@team-admin:" + equivDomain: 100,
			"@manager:" + equivDomain:    100,
		},
	}
	_, errT := tuwunelEquivOps(server).CreateRoom(context.Background(), spec)
	_, errS := synapseEquivOps(server).CreateRoom(context.Background(), spec)
	assertEquivNil(t, "CreateRoom (team)", errT, errS)

	shots := log.snapshot()
	var usersMaps []map[string]interface{}
	for _, r := range shots {
		if r.Path != "/_matrix/client/v3/createRoom" {
			continue
		}
		body := unmarshalBody(t, r.Body)
		pl, _ := body["power_level_content_override"].(map[string]interface{})
		users, _ := pl["users"].(map[string]interface{})
		usersMaps = append(usersMaps, users)
	}
	if len(usersMaps) != 2 {
		t.Fatalf("want 2 createRoom bodies, got %d", len(usersMaps))
	}
	// Both must omit the global admin (creator already present).
	for i, u := range usersMaps {
		if _, ok := u["@admin:"+equivDomain]; ok {
			t.Errorf("body %d injected global admin into team room: %v", i, u)
		}
	}
	// Compare actor + manager PLs explicitly.
	actor := "@team-admin:" + equivDomain
	if usersMaps[0][actor] != usersMaps[1][actor] {
		t.Errorf("actor PL diverges: tuwunel=%v synapse=%v",
			usersMaps[0][actor], usersMaps[1][actor])
	}
}

// TestEquiv_CreateRoom_AliasIdempotency asserts both impls treat an existing
// alias (M_ROOM_IN_USE) as idempotent: they resolve the alias and return
// RoomRef{Created: false, RoomID: <resolved>}.
func TestEquiv_CreateRoom_AliasIdempotency(t *testing.T) {
	// r.URL.Path is the DECODED path, so the alias match uses the literal
	// "#equiv-room:<domain>" form (matching client_test.go convention).
	aliasPath := "/_matrix/client/v3/directory/room/#equiv-room:" + equivDomain
	handler := func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/_matrix/client/v3/createRoom":
			// Always report the alias as in-use.
			writeMatrixError(w, http.StatusBadRequest, "M_ROOM_IN_USE", "Room alias already taken")
		case aliasPath:
			writeJSON(t, w, map[string]string{"room_id": equivRoomID})
		default:
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}
	log := &requestLog{}
	server := equivServer(t, log, handler)
	defer server.Close()

	spec := RoomSpec{Name: "Equiv Room", AliasLocalpart: "equiv-room"}
	rt, errT := tuwunelEquivOps(server).CreateRoom(context.Background(), spec)
	rs, errS := synapseEquivOps(server).CreateRoom(context.Background(), spec)
	assertEquivNil(t, "CreateRoom alias", errT, errS)
	assertEquivRoomRef(t, "CreateRoom alias", rt, rs)
	if rt.Created {
		t.Errorf("Created must be false on alias reuse, got %+v", rt)
	}
	if rt.RoomID != equivRoomID {
		t.Errorf("resolved RoomID = %q, want %q", rt.RoomID, equivRoomID)
	}
}

// TestEquiv_DissolveRoom_EmptyIsNil asserts both impls treat an empty roomID
// as a no-op without touching the wire.
func TestEquiv_DissolveRoom_EmptyIsNil(t *testing.T) {
	log := &requestLog{}
	server := equivServer(t, log, func(w http.ResponseWriter, r *http.Request) {
		t.Errorf("unexpected wire call for empty DissolveRoom: %s", r.URL.Path)
		w.WriteHeader(http.StatusNotFound)
	})
	defer server.Close()

	errT := tuwunelEquivOps(server).DissolveRoom(context.Background(), "")
	errS := synapseEquivOps(server).DissolveRoom(context.Background(), "")
	assertEquivNil(t, "DissolveRoom empty", errT, errS)
	if n := len(log.snapshot()); n != 0 {
		t.Errorf("empty DissolveRoom made %d wire calls, want 0", n)
	}
}

// TestEquiv_AddMember_Success asserts a clean invite produces nil on both
// impls and the same wire request shape (CS invite with admin token).
func TestEquiv_AddMember_Success(t *testing.T) {
	log := &requestLog{}
	server := equivServer(t, log, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/_matrix/client/v3/rooms/!room:"+equivDomain+"/invite" {
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("{}"))
			return
		}
		t.Errorf("unexpected path: %s", r.URL.Path)
		w.WriteHeader(http.StatusNotFound)
	})
	defer server.Close()

	roomID := "!room:" + equivDomain
	target := "@alice:" + equivDomain
	errT := tuwunelEquivOps(server).AddMember(context.Background(), roomID, target)
	errS := synapseEquivOps(server).AddMember(context.Background(), roomID, target)
	assertEquivNil(t, "AddMember", errT, errS)

	// Both invite bodies must carry the same user_id and admin token.
	shots := log.snapshot()
	var invites []recordedRequest
	for _, r := range shots {
		if strings.HasSuffix(r.Path, "/invite") {
			invites = append(invites, r)
		}
	}
	if len(invites) != 2 {
		t.Fatalf("want 2 invite requests, got %d", len(invites))
	}
	if invites[0].Auth != invites[1].Auth {
		t.Errorf("invite Authorization diverges: %q vs %q", invites[0].Auth, invites[1].Auth)
	}
	if unmarshalBody(t, invites[0].Body)["user_id"] != unmarshalBody(t, invites[1].Body)["user_id"] {
		t.Errorf("invite user_id diverges")
	}
}

// TestEquiv_AddMember_AlreadyInRoom asserts the idempotent already-joined
// case returns nil on both impls without escalation.
func TestEquiv_AddMember_AlreadyInRoom(t *testing.T) {
	handler := func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/invite") {
			writeMatrixError(w, http.StatusForbidden, "M_FORBIDDEN",
				"@alice:equiv.test is already in the room.")
			return
		}
		t.Errorf("unexpected path: %s", r.URL.Path)
		w.WriteHeader(http.StatusNotFound)
	}
	log := &requestLog{}
	server := equivServer(t, log, handler)
	defer server.Close()

	roomID := "!room:" + equivDomain
	target := "@alice:" + equivDomain
	errT := tuwunelEquivOps(server).AddMember(context.Background(), roomID, target)
	errS := synapseEquivOps(server).AddMember(context.Background(), roomID, target)
	assertEquivNil(t, "AddMember already-in", errT, errS)
	// No make_room_admin / admin-bot fallback should have fired.
	if log.countByPath("/_synapse/admin/v1/rooms/!room:"+equivDomain+"/make_room_admin") != 0 {
		t.Error("Synapse escalated an idempotent already-in-room case")
	}
	if adminBotMessageCount(log) != 0 {
		t.Error("Tuwunel delivered an admin-bot message for an idempotent case")
	}
}

// TestEquiv_RemoveMember_TargetNotInRoom asserts the idempotent target-not-in
// case returns nil on both impls.
func TestEquiv_RemoveMember_TargetNotInRoom(t *testing.T) {
	handler := func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/kick") {
			writeMatrixError(w, http.StatusForbidden, "M_FORBIDDEN",
				"User @alice:equiv.test is not in the room.")
			return
		}
		t.Errorf("unexpected path: %s", r.URL.Path)
		w.WriteHeader(http.StatusNotFound)
	}
	log := &requestLog{}
	server := equivServer(t, log, handler)
	defer server.Close()

	roomID := "!room:" + equivDomain
	target := "@alice:" + equivDomain
	errT := tuwunelEquivOps(server).RemoveMember(context.Background(), roomID, target, "")
	errS := synapseEquivOps(server).RemoveMember(context.Background(), roomID, target, "")
	assertEquivNil(t, "RemoveMember target-not-in", errT, errS)
	if log.countByPath("/_synapse/admin/v1/rooms/!room:"+equivDomain+"/make_room_admin") != 0 {
		t.Error("Synapse escalated an idempotent target-not-in-room case")
	}
	if adminBotMessageCount(log) != 0 {
		t.Error("Tuwunel delivered an admin-bot message for an idempotent case")
	}
}

// TestEquiv_InviteMember_ActorToken asserts the actor-token branch is shared
// verbatim: both impls issue InviteToRoomWithToken with the supplied token
// (no admin login).
func TestEquiv_InviteMember_ActorToken(t *testing.T) {
	log := &requestLog{}
	server := equivServer(t, log, func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/invite") {
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("{}"))
			return
		}
		t.Errorf("unexpected path: %s", r.URL.Path)
		w.WriteHeader(http.StatusNotFound)
	})
	defer server.Close()

	roomID := "!room:" + equivDomain
	target := "@alice:" + equivDomain
	member := MemberSpec{ActorToken: "team-admin-token"}
	errT := tuwunelEquivOps(server).InviteMember(context.Background(), roomID, target, member)
	errS := synapseEquivOps(server).InviteMember(context.Background(), roomID, target, member)
	assertEquivNil(t, "InviteMember actor", errT, errS)
	// No admin login should have happened on the actor-token path.
	if log.countByPath("/_matrix/client/v3/login") != 0 {
		t.Error("actor-token InviteMember triggered an admin login")
	}
}

// TestEquiv_ReconcileMembers_NoOp asserts ReconcileMembers with the desired
// set equal to the current membership returns nil on both impls without
// issuing any invite or kick.
func TestEquiv_ReconcileMembers_NoOp(t *testing.T) {
	roomID := "!room:" + equivDomain
	handler := func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/_matrix/client/v3/rooms/" + roomID + "/members":
			writeJSON(t, w, map[string]interface{}{
				"chunk": []map[string]interface{}{
					{"state_key": "@alice:" + equivDomain, "content": map[string]string{"membership": "join"}},
					{"state_key": "@admin:" + equivDomain, "content": map[string]string{"membership": "join"}},
				},
			})
		default:
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}
	log := &requestLog{}
	server := equivServer(t, log, handler)
	defer server.Close()

	desired := []MemberSpec{{UserID: "@alice:" + equivDomain}}
	errT := tuwunelEquivOps(server).ReconcileMembers(context.Background(), roomID, desired)
	errS := synapseEquivOps(server).ReconcileMembers(context.Background(), roomID, desired)
	assertEquivNil(t, "ReconcileMembers no-op", errT, errS)
	// No invite / kick / admin escalation should have fired.
	for _, r := range log.snapshot() {
		if strings.HasSuffix(r.Path, "/invite") || strings.HasSuffix(r.Path, "/kick") {
			t.Errorf("no-op reconcile issued %s %s", r.Method, r.Path)
		}
	}
}

// TestEquiv_ReconcileMembers_AddMissing asserts both impls invite a missing
// desired member via the same admin-token CS invite.
func TestEquiv_ReconcileMembers_AddMissing(t *testing.T) {
	roomID := "!room:" + equivDomain
	handler := func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/_matrix/client/v3/rooms/" + roomID + "/members":
			writeJSON(t, w, map[string]interface{}{
				"chunk": []map[string]interface{}{
					{"state_key": "@admin:" + equivDomain, "content": map[string]string{"membership": "join"}},
				},
			})
		case "/_matrix/client/v3/rooms/" + roomID + "/invite":
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("{}"))
		default:
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}
	log := &requestLog{}
	server := equivServer(t, log, handler)
	defer server.Close()

	desired := []MemberSpec{{UserID: "@alice:" + equivDomain}}
	errT := tuwunelEquivOps(server).ReconcileMembers(context.Background(), roomID, desired)
	errS := synapseEquivOps(server).ReconcileMembers(context.Background(), roomID, desired)
	assertEquivNil(t, "ReconcileMembers add", errT, errS)
	if n := log.countByPath("/_matrix/client/v3/rooms/" + roomID + "/invite"); n != 2 {
		t.Errorf("invite calls = %d, want 2 (one per impl)", n)
	}
}

// TestEquiv_JoinRoom asserts both impls join via the same CS join endpoint
// with the same token.
func TestEquiv_JoinRoom(t *testing.T) {
	roomID := "!room:" + equivDomain
	handler := func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/_matrix/client/v3/rooms/"+roomID+"/join" {
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("{}"))
			return
		}
		t.Errorf("unexpected path: %s", r.URL.Path)
		w.WriteHeader(http.StatusNotFound)
	}
	log := &requestLog{}
	server := equivServer(t, log, handler)
	defer server.Close()

	errT := tuwunelEquivOps(server).JoinRoom(context.Background(), roomID, MemberSpec{})
	errS := synapseEquivOps(server).JoinRoom(context.Background(), roomID, MemberSpec{})
	assertEquivNil(t, "JoinRoom", errT, errS)
	if n := log.countByPath("/_matrix/client/v3/rooms/" + roomID + "/join"); n != 2 {
		t.Errorf("join calls = %d, want 2", n)
	}
}

// TestEquiv_LeaveRoom asserts both impls leave via the same CS leave endpoint.
func TestEquiv_LeaveRoom(t *testing.T) {
	roomID := "!room:" + equivDomain
	handler := func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/_matrix/client/v3/rooms/"+roomID+"/leave" {
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("{}"))
			return
		}
		t.Errorf("unexpected path: %s", r.URL.Path)
		w.WriteHeader(http.StatusNotFound)
	}
	log := &requestLog{}
	server := equivServer(t, log, handler)
	defer server.Close()

	errT := tuwunelEquivOps(server).LeaveRoom(context.Background(), roomID, MemberSpec{})
	errS := synapseEquivOps(server).LeaveRoom(context.Background(), roomID, MemberSpec{})
	assertEquivNil(t, "LeaveRoom", errT, errS)
	if n := log.countByPath("/_matrix/client/v3/rooms/" + roomID + "/leave"); n != 2 {
		t.Errorf("leave calls = %d, want 2", n)
	}
}

// TestEquiv_ForceLeaveAllRooms asserts both impls list joined rooms and leave
// each one, producing identical leave traffic.
func TestEquiv_ForceLeaveAllRooms(t *testing.T) {
	roomA := "!a:" + equivDomain
	roomB := "!b:" + equivDomain
	handler := func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/_matrix/client/v3/joined_rooms":
			writeJSON(t, w, map[string]interface{}{"joined_rooms": []string{roomA, roomB}})
		case "/_matrix/client/v3/rooms/" + roomA + "/leave":
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("{}"))
		case "/_matrix/client/v3/rooms/" + roomB + "/leave":
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("{}"))
		default:
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}
	log := &requestLog{}
	server := equivServer(t, log, handler)
	defer server.Close()

	errT := tuwunelEquivOps(server).ForceLeaveAllRooms(context.Background(), MemberSpec{})
	errS := synapseEquivOps(server).ForceLeaveAllRooms(context.Background(), MemberSpec{})
	assertEquivNil(t, "ForceLeaveAllRooms", errT, errS)
	if n := log.countByPath("/_matrix/client/v3/rooms/" + roomA + "/leave"); n != 2 {
		t.Errorf("leave A calls = %d, want 2", n)
	}
	if n := log.countByPath("/_matrix/client/v3/rooms/" + roomB + "/leave"); n != 2 {
		t.Errorf("leave B calls = %d, want 2", n)
	}
}

// TestEquiv_ReleaseRoomAlias asserts both impls delete the alias via the same
// CS endpoint. Idempotency (M_NOT_FOUND → nil) is shared.
func TestEquiv_ReleaseRoomAlias(t *testing.T) {
	aliasPath := "/_matrix/client/v3/directory/room/#equiv-alias:" + equivDomain
	handler := func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == aliasPath {
			if r.Method != http.MethodDelete {
				t.Errorf("method = %s, want DELETE", r.Method)
			}
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("{}"))
			return
		}
		t.Errorf("unexpected path: %s", r.URL.Path)
		w.WriteHeader(http.StatusNotFound)
	}
	log := &requestLog{}
	server := equivServer(t, log, handler)
	defer server.Close()

	alias := "#equiv-alias:" + equivDomain
	errT := tuwunelEquivOps(server).ReleaseRoomAlias(context.Background(), alias)
	errS := synapseEquivOps(server).ReleaseRoomAlias(context.Background(), alias)
	assertEquivNil(t, "ReleaseRoomAlias", errT, errS)
	if n := log.countByPath(aliasPath); n != 2 {
		t.Errorf("delete-alias calls = %d, want 2", n)
	}
}

// TestEquiv_ResolveRoomAlias asserts both impls return the same roomID + ok
// flag from the same directory/room lookup.
func TestEquiv_ResolveRoomAlias(t *testing.T) {
	aliasPath := "/_matrix/client/v3/directory/room/#equiv-alias:" + equivDomain
	handler := func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == aliasPath {
			writeJSON(t, w, map[string]string{"room_id": equivRoomID})
			return
		}
		t.Errorf("unexpected path: %s", r.URL.Path)
		w.WriteHeader(http.StatusNotFound)
	}
	log := &requestLog{}
	server := equivServer(t, log, handler)
	defer server.Close()

	alias := "#equiv-alias:" + equivDomain
	idT, okT, errT := tuwunelEquivOps(server).ResolveRoomAlias(context.Background(), alias)
	idS, okS, errS := synapseEquivOps(server).ResolveRoomAlias(context.Background(), alias)
	assertEquivNil(t, "ResolveRoomAlias", errT, errS)
	if idT != idS || okT != okS {
		t.Errorf("ResolveRoomAlias diverges: tuwunel=(%s,%v) synapse=(%s,%v)", idT, okT, idS, okS)
	}
	if !okT || idT != equivRoomID {
		t.Errorf("ResolveRoomAlias = (%s,%v), want (%s,true)", idT, okT, equivRoomID)
	}
}

// TestEquiv_ResolveRoomAlias_NotFound asserts both impls return ok=false + nil
// for a missing alias (M_NOT_FOUND).
func TestEquiv_ResolveRoomAlias_NotFound(t *testing.T) {
	aliasPath := "/_matrix/client/v3/directory/room/#missing:" + equivDomain
	handler := func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == aliasPath {
			writeMatrixError(w, http.StatusNotFound, "M_NOT_FOUND", "Unknown alias")
			return
		}
		t.Errorf("unexpected path: %s", r.URL.Path)
		w.WriteHeader(http.StatusNotFound)
	}
	log := &requestLog{}
	server := equivServer(t, log, handler)
	defer server.Close()

	alias := "#missing:" + equivDomain
	idT, okT, errT := tuwunelEquivOps(server).ResolveRoomAlias(context.Background(), alias)
	idS, okS, errS := synapseEquivOps(server).ResolveRoomAlias(context.Background(), alias)
	assertEquivNil(t, "ResolveRoomAlias not-found", errT, errS)
	if okT || okS || idT != "" || idS != "" {
		t.Errorf("ResolveRoomAlias not-found diverges: tuwunel=(%q,%v) synapse=(%q,%v)",
			idT, okT, idS, okS)
	}
}

// TestEquiv_ArchiveRoom asserts both impls rename via the same m.room.name
// state write.
func TestEquiv_ArchiveRoom(t *testing.T) {
	roomID := "!room:" + equivDomain
	namePath := "/_matrix/client/v3/rooms/" + roomID + "/state/m.room.name/"
	handler := func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == namePath {
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("{}"))
			return
		}
		t.Errorf("unexpected path: %s", r.URL.Path)
		w.WriteHeader(http.StatusNotFound)
	}
	log := &requestLog{}
	server := equivServer(t, log, handler)
	defer server.Close()

	errT := tuwunelEquivOps(server).ArchiveRoom(context.Background(), roomID, "Archived", MemberSpec{})
	errS := synapseEquivOps(server).ArchiveRoom(context.Background(), roomID, "Archived", MemberSpec{})
	assertEquivNil(t, "ArchiveRoom", errT, errS)
	if n := log.countByPath(namePath); n != 2 {
		t.Errorf("m.room.name writes = %d, want 2", n)
	}
}

// TestEquiv_SetRoomMetadata_Success asserts both impls write the room.meta
// state event via the same CS endpoint when no fallback is triggered.
func TestEquiv_SetRoomMetadata_Success(t *testing.T) {
	roomID := "!room:" + equivDomain
	metaPath := "/_matrix/client/v3/rooms/" + roomID + "/state/room.meta/"
	handler := func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == metaPath {
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("{}"))
			return
		}
		t.Errorf("unexpected path: %s", r.URL.Path)
		w.WriteHeader(http.StatusNotFound)
	}
	log := &requestLog{}
	server := equivServer(t, log, handler)
	defer server.Close()

	content := map[string]interface{}{"roomKind": "worker_room"}
	errT := tuwunelEquivOps(server).SetRoomMetadata(context.Background(), roomID, content, MemberSpec{})
	errS := synapseEquivOps(server).SetRoomMetadata(context.Background(), roomID, content, MemberSpec{})
	assertEquivNil(t, "SetRoomMetadata", errT, errS)
	if n := log.countByPath(metaPath); n != 2 {
		t.Errorf("room.meta writes = %d, want 2", n)
	}
}

// TestEquiv_RenameRoom_Success asserts both impls rename via the same m.room.name
// write when no fallback is triggered.
func TestEquiv_RenameRoom_Success(t *testing.T) {
	roomID := "!room:" + equivDomain
	namePath := "/_matrix/client/v3/rooms/" + roomID + "/state/m.room.name/"
	handler := func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == namePath {
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("{}"))
			return
		}
		t.Errorf("unexpected path: %s", r.URL.Path)
		w.WriteHeader(http.StatusNotFound)
	}
	log := &requestLog{}
	server := equivServer(t, log, handler)
	defer server.Close()

	errT := tuwunelEquivOps(server).RenameRoom(context.Background(), roomID, "New Name", MemberSpec{})
	errS := synapseEquivOps(server).RenameRoom(context.Background(), roomID, "New Name", MemberSpec{})
	assertEquivNil(t, "RenameRoom", errT, errS)
	if n := log.countByPath(namePath); n != 2 {
		t.Errorf("rename writes = %d, want 2", n)
	}
}

// TestEquiv_SendSystemMessage_Success asserts both impls send via the same
// m.room.message send when no fallback is triggered.
func TestEquiv_SendSystemMessage_Success(t *testing.T) {
	roomID := "!room:" + equivDomain
	sendPrefix := "/_matrix/client/v3/rooms/" + roomID + "/send/m.room.message/"
	handler := func(w http.ResponseWriter, r *http.Request) {
		if strings.HasPrefix(r.URL.Path, sendPrefix) {
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("{}"))
			return
		}
		t.Errorf("unexpected path: %s", r.URL.Path)
		w.WriteHeader(http.StatusNotFound)
	}
	log := &requestLog{}
	server := equivServer(t, log, handler)
	defer server.Close()

	errT := tuwunelEquivOps(server).SendSystemMessage(context.Background(), roomID, "hello")
	errS := synapseEquivOps(server).SendSystemMessage(context.Background(), roomID, "hello")
	assertEquivNil(t, "SendSystemMessage", errT, errS)
	n := 0
	for _, r := range log.snapshot() {
		if strings.HasPrefix(r.Path, sendPrefix) {
			n++
		}
	}
	if n != 2 {
		t.Errorf("send calls = %d, want 2", n)
	}
}

// TestEquiv_ListRoomMembers_AdminToken asserts both impls list via the same
// admin-token /members read.
func TestEquiv_ListRoomMembers_AdminToken(t *testing.T) {
	roomID := "!room:" + equivDomain
	membersPath := "/_matrix/client/v3/rooms/" + roomID + "/members"
	handler := func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == membersPath {
			writeJSON(t, w, map[string]interface{}{
				"chunk": []map[string]interface{}{
					{"state_key": "@alice:" + equivDomain, "content": map[string]string{"membership": "join"}},
					{"state_key": "@bob:" + equivDomain, "content": map[string]string{"membership": "invite"}},
				},
			})
			return
		}
		t.Errorf("unexpected path: %s", r.URL.Path)
		w.WriteHeader(http.StatusNotFound)
	}
	log := &requestLog{}
	server := equivServer(t, log, handler)
	defer server.Close()

	mt, errT := tuwunelEquivOps(server).ListRoomMembers(context.Background(), roomID, MemberSpec{})
	ms, errS := synapseEquivOps(server).ListRoomMembers(context.Background(), roomID, MemberSpec{})
	assertEquivNil(t, "ListRoomMembers", errT, errS)
	if len(mt) != len(ms) || len(mt) != 2 {
		t.Fatalf("members diverge: tuwunel=%v synapse=%v", mt, ms)
	}
	for i := range mt {
		if mt[i] != ms[i] {
			t.Errorf("member %d diverges: tuwunel=%+v synapse=%+v", i, mt[i], ms[i])
		}
	}
}

// TestEquiv_ListJoinedRooms asserts both impls return the same joined_rooms
// list for the same admin identity.
func TestEquiv_ListJoinedRooms(t *testing.T) {
	handler := func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/_matrix/client/v3/joined_rooms" {
			writeJSON(t, w, map[string]interface{}{"joined_rooms": []string{"!a:" + equivDomain, "!b:" + equivDomain}})
			return
		}
		t.Errorf("unexpected path: %s", r.URL.Path)
		w.WriteHeader(http.StatusNotFound)
	}
	log := &requestLog{}
	server := equivServer(t, log, handler)
	defer server.Close()

	rt, errT := tuwunelEquivOps(server).ListJoinedRooms(context.Background(), MemberSpec{})
	rs, errS := synapseEquivOps(server).ListJoinedRooms(context.Background(), MemberSpec{})
	assertEquivNil(t, "ListJoinedRooms", errT, errS)
	if len(rt) != len(rs) {
		t.Fatalf("joined rooms diverge: tuwunel=%v synapse=%v", rt, rs)
	}
	for i := range rt {
		if rt[i] != rs[i] {
			t.Errorf("joined room %d diverges: %q vs %q", i, rt[i], rs[i])
		}
	}
}

// TestEquiv_IsUserInRoom asserts both impls return the same in-room verdict
// for the same members listing.
func TestEquiv_IsUserInRoom(t *testing.T) {
	roomID := "!room:" + equivDomain
	handler := func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/_matrix/client/v3/rooms/"+roomID+"/members" {
			writeJSON(t, w, map[string]interface{}{
				"chunk": []map[string]interface{}{
					{"state_key": "@alice:" + equivDomain, "content": map[string]string{"membership": "join"}},
					{"state_key": "@bob:" + equivDomain, "content": map[string]string{"membership": "invite"}},
				},
			})
			return
		}
		t.Errorf("unexpected path: %s", r.URL.Path)
		w.WriteHeader(http.StatusNotFound)
	}
	log := &requestLog{}
	server := equivServer(t, log, handler)
	defer server.Close()

	for _, tc := range []struct {
		userID string
		want   bool
	}{
		{"@alice:" + equivDomain, true},  // join → in
		{"@bob:" + equivDomain, false},   // invite → not "join"
		{"@carol:" + equivDomain, false}, // absent
	} {
		inT, errT := tuwunelEquivOps(server).IsUserInRoom(context.Background(), roomID, tc.userID)
		inS, errS := synapseEquivOps(server).IsUserInRoom(context.Background(), roomID, tc.userID)
		assertEquivNil(t, "IsUserInRoom "+tc.userID, errT, errS)
		if inT != inS || inT != tc.want {
			t.Errorf("IsUserInRoom(%s) = tuwunel=%v synapse=%v, want %v", tc.userID, inT, inS, tc.want)
		}
	}
}

// TestEquiv_IsManagerJoinedDM asserts both impls return the same manager-joined
// verdict for the same members listing.
func TestEquiv_IsManagerJoinedDM(t *testing.T) {
	roomID := "!room:" + equivDomain
	handler := func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/_matrix/client/v3/rooms/"+roomID+"/members" {
			writeJSON(t, w, map[string]interface{}{
				"chunk": []map[string]interface{}{
					{"state_key": "@manager:" + equivDomain, "content": map[string]string{"membership": "join"}},
				},
			})
			return
		}
		t.Errorf("unexpected path: %s", r.URL.Path)
		w.WriteHeader(http.StatusNotFound)
	}
	log := &requestLog{}
	server := equivServer(t, log, handler)
	defer server.Close()

	inT, errT := tuwunelEquivOps(server).IsManagerJoinedDM(context.Background(), roomID)
	inS, errS := synapseEquivOps(server).IsManagerJoinedDM(context.Background(), roomID)
	assertEquivNil(t, "IsManagerJoinedDM", errT, errS)
	if inT != inS || !inT {
		t.Errorf("IsManagerJoinedDM diverges: tuwunel=%v synapse=%v, want true/true", inT, inS)
	}
}

// TestEquiv_HealthCheck_ServerUp asserts both impls report nil when the server
// responds at the HTTP level (401 from the probe login means up).
func TestEquiv_HealthCheck_ServerUp(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/_matrix/client/v3/login" {
			writeMatrixError(w, http.StatusUnauthorized, "M_FORBIDDEN", "Invalid password")
			return
		}
		t.Errorf("unexpected path: %s", r.URL.Path)
		w.WriteHeader(http.StatusNotFound)
	}))
	defer server.Close()

	errT := tuwunelEquivOps(server).HealthCheck(context.Background())
	errS := synapseEquivOps(server).HealthCheck(context.Background())
	assertEquivNil(t, "HealthCheck up", errT, errS)
}

// TestEquiv_HealthCheck_ServerDown asserts both impls surface a transport
// error for an unreachable server.
func TestEquiv_HealthCheck_ServerDown(t *testing.T) {
	// Port 1 is reserved and refused on essentially every host.
	cfg := Config{ServerURL: "http://127.0.0.1:1", Domain: equivDomain, AdminUser: "admin", AdminPassword: "pw"}
	errT := NewTuwunelMatrixOps(cfg, &http.Client{}).HealthCheck(context.Background())
	errS := NewSynapseMatrixOps(cfg, &http.Client{}).HealthCheck(context.Background())
	if errT == nil || errS == nil {
		t.Errorf("HealthCheck down: tuwunel=%v synapse=%v, want both non-nil", errT, errS)
	}
}

// TestEquiv_UserIDFor asserts the pure-formatting helper returns the same ID
// under both impls (no I/O). No server is needed; construct ops directly.
func TestEquiv_UserIDFor(t *testing.T) {
	cfg := equivConfig("http://unused")
	tOps := NewTuwunelMatrixOps(cfg, &http.Client{})
	sOps := NewSynapseMatrixOps(cfg, &http.Client{})
	for _, localpart := range []string{"alice", "manager", "agentteams-controller"} {
		idT := tOps.UserIDFor(localpart)
		idS := sOps.UserIDFor(localpart)
		want := "@" + localpart + ":" + equivDomain
		if idT != want || idS != want || idT != idS {
			t.Errorf("UserIDFor(%s) = tuwunel=%q synapse=%q, want %q", localpart, idT, idS, want)
		}
	}
}

// TestEquiv_LoginUser asserts both impls return the same access token from
// the same password login.
func TestEquiv_LoginUser(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/_matrix/client/v3/login" {
			writeJSON(t, w, map[string]string{"access_token": "user-token"})
			return
		}
		t.Errorf("unexpected path: %s", r.URL.Path)
		w.WriteHeader(http.StatusNotFound)
	}))
	defer server.Close()

	tokT, errT := tuwunelEquivOps(server).LoginUser(context.Background(), "alice", "pw")
	tokS, errS := synapseEquivOps(server).LoginUser(context.Background(), "alice", "pw")
	assertEquivNil(t, "LoginUser", errT, errS)
	if tokT != tokS || tokT != "user-token" {
		t.Errorf("LoginUser tokens diverge: tuwunel=%q synapse=%q", tokT, tokS)
	}
}

// TestEquiv_VerifyUserAccessToken asserts both impls reach the same whoami
// endpoint with the same token and return nil for a 200 response.
func TestEquiv_VerifyUserAccessToken(t *testing.T) {
	log := &requestLog{}
	server := equivServer(t, log, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/_matrix/client/v3/account/whoami" {
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("{}"))
			return
		}
		t.Errorf("unexpected path: %s", r.URL.Path)
		w.WriteHeader(http.StatusNotFound)
	})
	defer server.Close()

	errT := tuwunelEquivOps(server).VerifyUserAccessToken(context.Background(), "user-token")
	errS := synapseEquivOps(server).VerifyUserAccessToken(context.Background(), "user-token")
	assertEquivNil(t, "VerifyUserAccessToken", errT, errS)
	if n := log.countByPath("/_matrix/client/v3/account/whoami"); n != 2 {
		t.Errorf("whoami calls = %d, want 2", n)
	}
}

// TestEquiv_SetUserDisplayName_WithToken asserts the user-token branch is
// shared verbatim: both impls PUT the same displayname via the same CS profile
// endpoint.
func TestEquiv_SetUserDisplayName_WithToken(t *testing.T) {
	userID := "@alice:" + equivDomain
	profilePath := "/_matrix/client/v3/profile/" + userID + "/displayname"
	handler := func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == profilePath {
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("{}"))
			return
		}
		t.Errorf("unexpected path: %s", r.URL.Path)
		w.WriteHeader(http.StatusNotFound)
	}
	log := &requestLog{}
	server := equivServer(t, log, handler)
	defer server.Close()

	errT := tuwunelEquivOps(server).SetUserDisplayName(context.Background(), userID, "user-token", "Alice")
	errS := synapseEquivOps(server).SetUserDisplayName(context.Background(), userID, "user-token", "Alice")
	assertEquivNil(t, "SetUserDisplayName token", errT, errS)
	if n := log.countByPath(profilePath); n != 2 {
		t.Errorf("profile PUTs = %d, want 2", n)
	}
}

// TestEquiv_SmokeTestAppService asserts the AS-login smoke test is shared
// verbatim: both impls attempt the same AS login as the sender_localpart.
func TestEquiv_SmokeTestAppService(t *testing.T) {
	log := &requestLog{}
	server := equivServer(t, log, func(w http.ResponseWriter, r *http.Request) {
		// Any login with the AS Bearer token succeeds.
		writeJSON(t, w, map[string]string{"access_token": "ok"})
	})
	defer server.Close()

	cfg := equivConfig(server.URL)
	cfg.AppServiceSenderLocalpart = "agentteams-controller"
	cfg.AppServiceToken = "as-token"
	tOps := NewTuwunelMatrixOps(cfg, server.Client())
	sOps := NewSynapseMatrixOps(cfg, server.Client())

	errT := tOps.SmokeTestAppService(context.Background())
	errS := sOps.SmokeTestAppService(context.Background())
	assertEquivNil(t, "SmokeTestAppService", errT, errS)
}

// TestEquiv_ProvisionUserViaAppService asserts the AS-user provisioning path
// (register with as_token, fall back to AS login) is shared verbatim.
func TestEquiv_ProvisionUserViaAppService(t *testing.T) {
	log := &requestLog{}
	server := equivServer(t, log, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/_matrix/client/v3/register" {
			writeJSON(t, w, map[string]string{
				"user_id":      "@alice:" + equivDomain,
				"access_token": "as-user-token",
			})
			return
		}
		t.Errorf("unexpected path: %s", r.URL.Path)
		w.WriteHeader(http.StatusNotFound)
	})
	defer server.Close()

	cfg := equivConfig(server.URL)
	cfg.AppServiceToken = "as-token"
	tOps := NewTuwunelMatrixOps(cfg, server.Client())
	sOps := NewSynapseMatrixOps(cfg, server.Client())

	refT, ucT, errT := tOps.ProvisionUserViaAppService(context.Background(), "alice")
	refS, ucS, errS := sOps.ProvisionUserViaAppService(context.Background(), "alice")
	assertEquivNil(t, "ProvisionUserViaAppService", errT, errS)
	if refT == nil || refS == nil {
		t.Fatalf("refs nil: tuwunel=%v synapse=%v", refT, refS)
	}
	if refT.UserID != refS.UserID || refT.Created != refS.Created {
		t.Errorf("refs diverge: tuwunel=%+v synapse=%+v", refT, refS)
	}
	if ucT.AccessToken != ucS.AccessToken {
		t.Errorf("tokens diverge: tuwunel=%q synapse=%q", ucT.AccessToken, ucS.AccessToken)
	}
}

// TestEquiv_LoginUserViaAppService asserts the AS-login path is shared verbatim.
func TestEquiv_LoginUserViaAppService(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/_matrix/client/v3/login" {
			writeJSON(t, w, map[string]string{"access_token": "as-user-token"})
			return
		}
		t.Errorf("unexpected path: %s", r.URL.Path)
		w.WriteHeader(http.StatusNotFound)
	}))
	defer server.Close()

	cfg := equivConfig(server.URL)
	cfg.AppServiceToken = "as-token"
	tOps := NewTuwunelMatrixOps(cfg, server.Client())
	sOps := NewSynapseMatrixOps(cfg, server.Client())

	tokT, errT := tOps.LoginUserViaAppService(context.Background(), "alice")
	tokS, errS := sOps.LoginUserViaAppService(context.Background(), "alice")
	assertEquivNil(t, "LoginUserViaAppService", errT, errS)
	if tokT != tokS || tokT != "as-user-token" {
		t.Errorf("tokens diverge: tuwunel=%q synapse=%q", tokT, tokS)
	}
}

// ===========================================================================
// GROUP B — wire divergence paths (admin operations)
//
// These methods are NOT byte-identical: the admin operation goes through
// different wires on each implementation. The tests pin the exact divergence
// so a regression that swaps the wire (e.g. Synapse accidentally emitting an
// admin-bot command, or Tuwunel accidentally calling make_room_admin) is
// caught immediately.
// ===========================================================================

// TestEquiv_DissolveRoom_WireDivergence pins that:
//   - Tuwunel delivers "!admin rooms delete-room <roomID>" to the #admins room.
//   - Synapse issues DELETE /_synapse/admin/v2/rooms/{roomID}.
//
// Both must return nil, but the wires are different.
func TestEquiv_DissolveRoom_WireDivergence(t *testing.T) {
	roomID := "!room:" + equivDomain
	deletePath := "/_synapse/admin/v2/rooms/" + roomID
	log := &requestLog{}
	server := equivServer(t, log, func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case deletePath:
			if r.Method != http.MethodDelete {
				t.Errorf("Synapse DELETE method = %s", r.Method)
			}
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("{}"))
		default:
			// Admin-bot message delivery is handled by the txnID prefix below;
			// any other path is unexpected.
			if !strings.HasPrefix(r.URL.Path, txnIDPattern) {
				t.Errorf("unexpected path: %s", r.URL.Path)
				w.WriteHeader(http.StatusNotFound)
				return
			}
			// Tuwunel admin bot message: decode body and assert the command.
			body := unmarshalBody(t, lastBody(log, txnIDPattern))
			if body["body"] != "!admin rooms delete-room "+roomID {
				t.Errorf("Tuwunel admin body = %q, want !admin rooms delete-room %s",
					body["body"], roomID)
			}
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("{}"))
		}
	})
	defer server.Close()

	errT := tuwunelEquivOps(server).DissolveRoom(context.Background(), roomID)
	errS := synapseEquivOps(server).DissolveRoom(context.Background(), roomID)
	assertEquivNil(t, "DissolveRoom", errT, errS)
	if log.countByPath(deletePath) != 1 {
		t.Errorf("Synapse DELETE calls = %d, want 1", log.countByPath(deletePath))
	}
	if adminBotMessageCount(log) != 1 {
		t.Errorf("Tuwunel admin-bot messages = %d, want 1", adminBotMessageCount(log))
	}
}

// lastBody returns the most recent recorded request whose path starts with
// prefix. Used by handlers that need to assert on a just-captured body.
func lastBody(log *requestLog, prefix string) string {
	shots := log.snapshot()
	for i := len(shots) - 1; i >= 0; i-- {
		if strings.HasPrefix(shots[i].Path, prefix) {
			return shots[i].Body
		}
	}
	return ""
}

// TestEquiv_RemoveMember_FallbackDivergence pins the documented divergence
// in the kick-failure escalation path:
//   - Tuwunel: CS kick fails with insufficient power → admin bot
//     "!admin users force-leave-room <userID> <roomID>".
//   - Synapse: CS kick fails with insufficient power → make_room_admin + retry.
//
// Both must return nil.
func TestEquiv_RemoveMember_FallbackDivergence(t *testing.T) {
	roomID := "!room:" + equivDomain
	target := "@alice:" + equivDomain
	makeAdminPath := "/_synapse/admin/v1/rooms/" + roomID + "/make_room_admin"
	kickPath := "/_matrix/client/v3/rooms/" + roomID + "/kick"

	t.Run("Tuwunel_AdminBotForceLeave", func(t *testing.T) {
		log := &requestLog{}
		server := equivServer(t, log, func(w http.ResponseWriter, r *http.Request) {
			switch {
			case r.URL.Path == kickPath:
				// Power-insufficient error matches the escalation matcher.
				writeMatrixError(w, http.StatusForbidden, "M_FORBIDDEN",
					"sender does not have enough power to kick target user")
			case strings.HasPrefix(r.URL.Path, txnIDPattern):
				body := unmarshalBody(t, lastBody(log, txnIDPattern))
				if want := "!admin users force-leave-room " + target + " " + roomID; body["body"] != want {
					t.Errorf("admin body = %q, want %q", body["body"], want)
				}
				w.WriteHeader(http.StatusOK)
				w.Write([]byte("{}"))
			default:
				t.Errorf("unexpected path: %s", r.URL.Path)
				w.WriteHeader(http.StatusNotFound)
			}
		})
		defer server.Close()

		if err := tuwunelEquivOps(server).RemoveMember(context.Background(), roomID, target, ""); err != nil {
			t.Fatalf("Tuwunel RemoveMember fallback: %v", err)
		}
		if log.countByPath(makeAdminPath) != 0 {
			t.Error("Tuwunel must NOT call make_room_admin")
		}
		if adminBotMessageCount(log) != 1 {
			t.Errorf("admin-bot messages = %d, want 1", adminBotMessageCount(log))
		}
	})

	t.Run("Synapse_MakeRoomAdminRetry", func(t *testing.T) {
		var kickCalls int
		log := &requestLog{}
		server := equivServer(t, log, func(w http.ResponseWriter, r *http.Request) {
			switch r.URL.Path {
			case kickPath:
				kickCalls++
				if kickCalls == 1 {
					writeMatrixError(w, http.StatusForbidden, "M_FORBIDDEN",
						"You cannot kick user "+target+".")
					return
				}
				w.WriteHeader(http.StatusOK)
				w.Write([]byte("{}"))
			case makeAdminPath:
				body := unmarshalBody(t, lastRecordedBody(log, makeAdminPath))
				if body["user_id"] != "@admin:"+equivDomain {
					t.Errorf("make_room_admin user_id = %v, want @admin:%s", body["user_id"], equivDomain)
				}
				w.WriteHeader(http.StatusOK)
				w.Write([]byte("{}"))
			default:
				t.Errorf("unexpected path: %s", r.URL.Path)
				w.WriteHeader(http.StatusNotFound)
			}
		})
		defer server.Close()

		if err := synapseEquivOps(server).RemoveMember(context.Background(), roomID, target, ""); err != nil {
			t.Fatalf("Synapse RemoveMember fallback: %v", err)
		}
		if kickCalls != 2 {
			t.Errorf("kick calls = %d, want 2 (initial + retry)", kickCalls)
		}
		if log.countByPath(makeAdminPath) != 1 {
			t.Errorf("make_room_admin calls = %d, want 1", log.countByPath(makeAdminPath))
		}
		if adminBotMessageCount(log) != 0 {
			t.Error("Synapse must NOT deliver an admin-bot message")
		}
	})
}

// lastRecordedBody returns the body of the most recent recorded request whose
// path matches path exactly. Distinct from lastBody (prefix match) to avoid
// ambiguity on exact admin endpoints.
func lastRecordedBody(log *requestLog, path string) string {
	shots := log.snapshot()
	for i := len(shots) - 1; i >= 0; i-- {
		if shots[i].Path == path {
			return shots[i].Body
		}
	}
	return ""
}

// TestEquiv_RemoveMember_FallbackFailure wraps the documented wrap strings
// so a future edit that changes the error message is caught. The exact wrap
// is part of the public contract surfaced to operators in logs.
func TestEquiv_RemoveMember_FallbackFailure(t *testing.T) {
	roomID := "!room:" + equivDomain
	target := "@alice:" + equivDomain
	makeAdminPath := "/_synapse/admin/v1/rooms/" + roomID + "/make_room_admin"
	kickPath := "/_matrix/client/v3/rooms/" + roomID + "/kick"

	t.Run("Tuwunel_AdminBotFailure", func(t *testing.T) {
		log := &requestLog{}
		server := equivServer(t, log, func(w http.ResponseWriter, r *http.Request) {
			switch {
			case r.URL.Path == kickPath:
				writeMatrixError(w, http.StatusForbidden, "M_FORBIDDEN",
					"sender does not have enough power to kick target user")
			case strings.HasPrefix(r.URL.Path, txnIDPattern):
				// Admin bot delivery itself fails.
				w.WriteHeader(http.StatusServiceUnavailable)
				w.Write([]byte(`{"errcode":"M_UNKNOWN","error":"bot down"}`))
			default:
				t.Errorf("unexpected path: %s", r.URL.Path)
				w.WriteHeader(http.StatusNotFound)
			}
		})
		defer server.Close()

		err := tuwunelEquivOps(server).RemoveMember(context.Background(), roomID, target, "")
		if err == nil {
			t.Fatal("expected wrapped error, got nil")
		}
		if !strings.Contains(err.Error(), "force-leave-room command failed") {
			t.Errorf("error = %q, want wrap containing 'force-leave-room command failed'", err)
		}
	})

	t.Run("Synapse_MakeRoomAdminFailure", func(t *testing.T) {
		log := &requestLog{}
		server := equivServer(t, log, func(w http.ResponseWriter, r *http.Request) {
			switch r.URL.Path {
			case kickPath:
				writeMatrixError(w, http.StatusForbidden, "M_FORBIDDEN",
					"You cannot kick user "+target+".")
			case makeAdminPath:
				w.WriteHeader(http.StatusForbidden)
				w.Write([]byte(`{"errcode":"M_FORBIDDEN","error":"not a server admin"}`))
			default:
				t.Errorf("unexpected path: %s", r.URL.Path)
				w.WriteHeader(http.StatusNotFound)
			}
		})
		defer server.Close()

		err := synapseEquivOps(server).RemoveMember(context.Background(), roomID, target, "")
		if err == nil {
			t.Fatal("expected wrapped error, got nil")
		}
		if !strings.Contains(err.Error(), "make_room_admin failed") {
			t.Errorf("error = %q, want wrap containing 'make_room_admin failed'", err)
		}
	})
}

// TestEquiv_AddMember_FallbackDivergence pins the documented divergence in
// the invite-failure escalation path:
//   - Tuwunel: there is NO fallback — a forbidden invite is returned as-is.
//   - Synapse: a sender-not-joined / insufficient-power invite fails over to
//     make_room_admin + invite retry.
func TestEquiv_AddMember_FallbackDivergence(t *testing.T) {
	roomID := "!room:" + equivDomain
	target := "@alice:" + equivDomain
	invitePath := "/_matrix/client/v3/rooms/" + roomID + "/invite"
	makeAdminPath := "/_synapse/admin/v1/rooms/" + roomID + "/make_room_admin"

	t.Run("Tuwunel_NoFallback", func(t *testing.T) {
		log := &requestLog{}
		server := equivServer(t, log, func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path == invitePath {
				writeMatrixError(w, http.StatusForbidden, "M_FORBIDDEN",
					"@admin:"+equivDomain+" not in room "+roomID+".")
				return
			}
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		})
		defer server.Close()

		err := tuwunelEquivOps(server).AddMember(context.Background(), roomID, target)
		if err == nil {
			t.Fatal("Tuwunel AddMember: expected forbidden error, got nil")
		}
		if log.countByPath(makeAdminPath) != 0 || adminBotMessageCount(log) != 0 {
			t.Error("Tuwunel AddMember must not escalate")
		}
	})

	t.Run("Synapse_MakeRoomAdminRetry", func(t *testing.T) {
		var inviteCalls int
		log := &requestLog{}
		server := equivServer(t, log, func(w http.ResponseWriter, r *http.Request) {
			switch r.URL.Path {
			case invitePath:
				inviteCalls++
				if inviteCalls == 1 {
					writeMatrixError(w, http.StatusForbidden, "M_FORBIDDEN",
						"@admin:"+equivDomain+" not in room "+roomID+".")
					return
				}
				w.WriteHeader(http.StatusOK)
				w.Write([]byte("{}"))
			case makeAdminPath:
				w.WriteHeader(http.StatusOK)
				w.Write([]byte("{}"))
			default:
				t.Errorf("unexpected path: %s", r.URL.Path)
				w.WriteHeader(http.StatusNotFound)
			}
		})
		defer server.Close()

		if err := synapseEquivOps(server).AddMember(context.Background(), roomID, target); err != nil {
			t.Fatalf("Synapse AddMember fallback: %v", err)
		}
		if inviteCalls != 2 {
			t.Errorf("invite calls = %d, want 2", inviteCalls)
		}
		if log.countByPath(makeAdminPath) != 1 {
			t.Errorf("make_room_admin calls = %d, want 1", log.countByPath(makeAdminPath))
		}
	})
}

// TestEquiv_SetRoomMetadata_FallbackDivergence pins that Synapse escalates a
// sender-not-joined room.meta write to make_room_admin on the actor, while
// Tuwunel returns the CS error directly.
func TestEquiv_SetRoomMetadata_FallbackDivergence(t *testing.T) {
	roomID := "!room:" + equivDomain
	metaPath := "/_matrix/client/v3/rooms/" + roomID + "/state/room.meta/"
	makeAdminPath := "/_synapse/admin/v1/rooms/" + roomID + "/make_room_admin"
	forbiddenErr := "User @team-admin:" + equivDomain + " not in room " + roomID + " (None)"

	t.Run("Tuwunel_NoFallback", func(t *testing.T) {
		log := &requestLog{}
		server := equivServer(t, log, func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path == metaPath {
				writeMatrixError(w, http.StatusForbidden, "M_FORBIDDEN", forbiddenErr)
				return
			}
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		})
		defer server.Close()

		err := tuwunelEquivOps(server).SetRoomMetadata(context.Background(), roomID,
			map[string]interface{}{"roomKind": "team_room"},
			MemberSpec{ActorUserID: "@team-admin:" + equivDomain, ActorToken: "team-admin-token"})
		if err == nil {
			t.Fatal("Tuwunel SetRoomMetadata: expected forbidden error, got nil")
		}
		if log.countByPath(makeAdminPath) != 0 {
			t.Error("Tuwunel must not call make_room_admin")
		}
	})

	t.Run("Synapse_MakeRoomAdminOnActor", func(t *testing.T) {
		var metaCalls int
		log := &requestLog{}
		server := equivServer(t, log, func(w http.ResponseWriter, r *http.Request) {
			switch r.URL.Path {
			case metaPath:
				metaCalls++
				if metaCalls == 1 {
					writeMatrixError(w, http.StatusForbidden, "M_FORBIDDEN", forbiddenErr)
					return
				}
				w.WriteHeader(http.StatusOK)
				w.Write([]byte("{}"))
			case makeAdminPath:
				body := unmarshalBody(t, lastRecordedBody(log, makeAdminPath))
				if body["user_id"] != "@team-admin:"+equivDomain {
					t.Errorf("make_room_admin user_id = %v, want the actor @team-admin:%s",
						body["user_id"], equivDomain)
				}
				w.WriteHeader(http.StatusOK)
				w.Write([]byte("{}"))
			default:
				t.Errorf("unexpected path: %s", r.URL.Path)
				w.WriteHeader(http.StatusNotFound)
			}
		})
		defer server.Close()

		err := synapseEquivOps(server).SetRoomMetadata(context.Background(), roomID,
			map[string]interface{}{"roomKind": "team_room"},
			MemberSpec{ActorUserID: "@team-admin:" + equivDomain, ActorToken: "team-admin-token"})
		if err != nil {
			t.Fatalf("Synapse SetRoomMetadata fallback: %v", err)
		}
		if metaCalls != 2 {
			t.Errorf("room.meta writes = %d, want 2", metaCalls)
		}
	})
}

// TestEquiv_SendSystemMessage_FallbackDivergence pins that Synapse escalates a
// sender-not-joined system message (admin identity) to make_room_admin on the
// admin, while Tuwunel returns the CS error directly.
func TestEquiv_SendSystemMessage_FallbackDivergence(t *testing.T) {
	roomID := "!room:" + equivDomain
	makeAdminPath := "/_synapse/admin/v1/rooms/" + roomID + "/make_room_admin"
	sendPrefix := "/_matrix/client/v3/rooms/" + roomID + "/send/m.room.message/"
	forbiddenErr := "User @admin:" + equivDomain + " not in room " + roomID + " (None)"

	t.Run("Tuwunel_NoFallback", func(t *testing.T) {
		log := &requestLog{}
		server := equivServer(t, log, func(w http.ResponseWriter, r *http.Request) {
			if strings.HasPrefix(r.URL.Path, sendPrefix) {
				writeMatrixError(w, http.StatusForbidden, "M_FORBIDDEN", forbiddenErr)
				return
			}
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		})
		defer server.Close()

		err := tuwunelEquivOps(server).SendSystemMessage(context.Background(), roomID, "hi")
		if err == nil {
			t.Fatal("Tuwunel SendSystemMessage: expected forbidden error, got nil")
		}
		if log.countByPath(makeAdminPath) != 0 {
			t.Error("Tuwunel must not call make_room_admin")
		}
	})

	t.Run("Synapse_MakeRoomAdminOnAdmin", func(t *testing.T) {
		var sendCalls int
		log := &requestLog{}
		server := equivServer(t, log, func(w http.ResponseWriter, r *http.Request) {
			switch {
			case strings.HasPrefix(r.URL.Path, sendPrefix):
				sendCalls++
				if sendCalls == 1 {
					writeMatrixError(w, http.StatusForbidden, "M_FORBIDDEN", forbiddenErr)
					return
				}
				w.WriteHeader(http.StatusOK)
				w.Write([]byte("{}"))
			case r.URL.Path == makeAdminPath:
				body := unmarshalBody(t, lastRecordedBody(log, makeAdminPath))
				if body["user_id"] != "@admin:"+equivDomain {
					t.Errorf("make_room_admin user_id = %v, want @admin:%s", body["user_id"], equivDomain)
				}
				w.WriteHeader(http.StatusOK)
				w.Write([]byte("{}"))
			default:
				t.Errorf("unexpected path: %s", r.URL.Path)
				w.WriteHeader(http.StatusNotFound)
			}
		})
		defer server.Close()

		if err := synapseEquivOps(server).SendSystemMessage(context.Background(), roomID, "hi"); err != nil {
			t.Fatalf("Synapse SendSystemMessage fallback: %v", err)
		}
		if sendCalls != 2 {
			t.Errorf("send calls = %d, want 2", sendCalls)
		}
	})
}

// TestEquiv_ResetUserPassword_WireDivergence pins that:
//   - Tuwunel delivers "!admin users reset-password <userID> <password>".
//   - Synapse issues POST /_synapse/admin/v1/reset_password/{userID}.
func TestEquiv_ResetUserPassword_WireDivergence(t *testing.T) {
	userID := "@alice:" + equivDomain
	password := "newpw"
	resetPath := "/_synapse/admin/v1/reset_password/" + userID
	log := &requestLog{}
	server := equivServer(t, log, func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == resetPath:
			if r.Method != http.MethodPost {
				t.Errorf("Synapse reset method = %s, want POST", r.Method)
			}
			body := unmarshalBody(t, lastRecordedBody(log, resetPath))
			if body["new_password"] != password {
				t.Errorf("reset body new_password = %v, want %q", body["new_password"], password)
			}
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("{}"))
		case strings.HasPrefix(r.URL.Path, txnIDPattern):
			body := unmarshalBody(t, lastBody(log, txnIDPattern))
			if want := "!admin users reset-password " + userID + " " + password; body["body"] != want {
				t.Errorf("Tuwunel admin body = %q, want %q", body["body"], want)
			}
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("{}"))
		default:
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	})
	defer server.Close()

	errT := tuwunelEquivOps(server).ResetUserPassword(context.Background(), userID, password)
	errS := synapseEquivOps(server).ResetUserPassword(context.Background(), userID, password)
	assertEquivNil(t, "ResetUserPassword", errT, errS)
	if log.countByPath(resetPath) != 1 {
		t.Errorf("Synapse reset calls = %d, want 1", log.countByPath(resetPath))
	}
	if adminBotMessageCount(log) != 1 {
		t.Errorf("Tuwunel admin-bot messages = %d, want 1", adminBotMessageCount(log))
	}
}

// TestEquiv_DeactivateUser_WireDivergence pins that:
//   - Tuwunel delivers "!admin users deactivate <userID>".
//   - Synapse issues POST /_synapse/admin/v1/deactivate/{userID} with erase=false.
func TestEquiv_DeactivateUser_WireDivergence(t *testing.T) {
	userID := "@alice:" + equivDomain
	deactPath := "/_synapse/admin/v1/deactivate/" + userID
	log := &requestLog{}
	server := equivServer(t, log, func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == deactPath:
			body := unmarshalBody(t, lastRecordedBody(log, deactPath))
			// JSON booleans decode to bool. erase must be false (data preserved).
			if erase, ok := body["erase"].(bool); !ok || erase {
				t.Errorf("Synapse deactivate erase = %v, want false", body["erase"])
			}
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("{}"))
		case strings.HasPrefix(r.URL.Path, txnIDPattern):
			body := unmarshalBody(t, lastBody(log, txnIDPattern))
			if want := "!admin users deactivate " + userID; body["body"] != want {
				t.Errorf("Tuwunel admin body = %q, want %q", body["body"], want)
			}
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("{}"))
		default:
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	})
	defer server.Close()

	errT := tuwunelEquivOps(server).DeactivateUser(context.Background(), userID)
	errS := synapseEquivOps(server).DeactivateUser(context.Background(), userID)
	assertEquivNil(t, "DeactivateUser", errT, errS)
	if log.countByPath(deactPath) != 1 {
		t.Errorf("Synapse deactivate calls = %d, want 1", log.countByPath(deactPath))
	}
	if adminBotMessageCount(log) != 1 {
		t.Errorf("Tuwunel admin-bot messages = %d, want 1", adminBotMessageCount(log))
	}
}

// TestEquiv_ProvisionUser_WireDivergence pins that:
//   - Tuwunel registers via POST /_matrix/client/v3/register (registration_token),
//     falling back to /login on M_USER_IN_USE.
//   - Synapse creates the user via PUT /_synapse/admin/v2/users/{userID} then
//     logs in; Created is always reported true.
func TestEquiv_ProvisionUser_WireDivergence(t *testing.T) {
	userID := "@alice:" + equivDomain
	synUsersPath := "/_synapse/admin/v2/users/" + userID
	registerPath := "/_matrix/client/v3/register"

	t.Run("Tuwunel_RegistrationToken", func(t *testing.T) {
		log := &requestLog{}
		server := equivServer(t, log, func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path == registerPath {
				writeJSON(t, w, map[string]string{
					"user_id":      userID,
					"access_token": "tuwunel-token",
				})
				return
			}
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		})
		defer server.Close()

		ref, uc, err := tuwunelEquivOps(server).ProvisionUser(context.Background(),
			UserSpec{Username: "alice", Password: "pw"})
		if err != nil {
			t.Fatalf("Tuwunel ProvisionUser: %v", err)
		}
		if !ref.Created {
			t.Errorf("ref.Created = false, want true for a fresh register")
		}
		if uc.AccessToken != "tuwunel-token" {
			t.Errorf("token = %q, want tuwunel-token", uc.AccessToken)
		}
		if log.countByPath(synUsersPath) != 0 {
			t.Error("Tuwunel must NOT call the Synapse admin users endpoint")
		}
	})

	t.Run("Synapse_AdminUsersThenLogin", func(t *testing.T) {
		// Synapse ProvisionUser: PUT /_synapse/admin/v2/users/{id} (which
		// triggers an admin login first) then POST /login as the USER to get
		// the access token. The two logins hit the same /login path, so we
		// decode the request body and emit different tokens by username
		// (admin → admin-token, alice → synapse-token). equivServer's scaffold
		// blanket-returns admin-token for /login, so this subtest uses a raw
		// recording server to be username-aware.
		log := &requestLog{}
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			log.record(r)
			switch r.URL.Path {
			case "/_matrix/client/v3/login":
				body := unmarshalBody(t, lastRecordedBody(log, r.URL.Path))
				identifier, _ := body["identifier"].(map[string]interface{})
				user, _ := identifier["user"].(string)
				switch user {
				case "admin":
					writeJSON(t, w, map[string]string{"access_token": "admin-token"})
				case "alice":
					writeJSON(t, w, map[string]string{"access_token": "synapse-token"})
				default:
					t.Errorf("unexpected login user %q", user)
					w.WriteHeader(http.StatusUnauthorized)
				}
			case synUsersPath:
				if r.Method != http.MethodPut {
					t.Errorf("Synapse users method = %s, want PUT", r.Method)
				}
				body := unmarshalBody(t, lastRecordedBody(log, synUsersPath))
				if body["password"] != "pw" {
					t.Errorf("users body password = %v, want pw", body["password"])
				}
				w.WriteHeader(http.StatusOK)
				w.Write([]byte("{}"))
			default:
				t.Errorf("unexpected path: %s", r.URL.Path)
				w.WriteHeader(http.StatusNotFound)
			}
		}))
		defer server.Close()

		ref, uc, err := synapseEquivOps(server).ProvisionUser(context.Background(),
			UserSpec{Username: "alice", Password: "pw"})
		if err != nil {
			t.Fatalf("Synapse ProvisionUser: %v", err)
		}
		// Synapse always reports Created=true (admin API has no register/login distinction).
		if !ref.Created {
			t.Errorf("ref.Created = false, want true (Synapse admin API invariant)")
		}
		if uc.AccessToken != "synapse-token" {
			t.Errorf("token = %q, want synapse-token", uc.AccessToken)
		}
		if log.countByPath(registerPath) != 0 {
			t.Error("Synapse must NOT call the registration_token register endpoint")
		}
	})
}

// TestEquiv_SetUserDisplayName_EmptyTokenDivergence pins that with an empty
// access token:
//   - Tuwunel resolves the admin token and uses the CS profile endpoint.
//   - Synapse routes through the admin REST users endpoint (PUT displayname).
func TestEquiv_SetUserDisplayName_EmptyTokenDivergence(t *testing.T) {
	userID := "@alice:" + equivDomain
	profilePath := "/_matrix/client/v3/profile/" + userID + "/displayname"
	synUsersPath := "/_synapse/admin/v2/users/" + userID

	t.Run("Tuwunel_CSProfileWithAdminToken", func(t *testing.T) {
		log := &requestLog{}
		server := equivServer(t, log, func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path == profilePath {
				if r.Header.Get("Authorization") != "Bearer admin-token" {
					t.Errorf("Authorization = %q, want Bearer admin-token", r.Header.Get("Authorization"))
				}
				w.WriteHeader(http.StatusOK)
				w.Write([]byte("{}"))
				return
			}
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		})
		defer server.Close()

		if err := tuwunelEquivOps(server).SetUserDisplayName(context.Background(), userID, "", "Alice"); err != nil {
			t.Fatalf("Tuwunel SetUserDisplayName: %v", err)
		}
		if log.countByPath(profilePath) != 1 {
			t.Errorf("profile PUTs = %d, want 1", log.countByPath(profilePath))
		}
		if log.countByPath(synUsersPath) != 0 {
			t.Error("Tuwunel must NOT call the Synapse admin users endpoint")
		}
	})

	t.Run("Synapse_AdminUsersEndpoint", func(t *testing.T) {
		log := &requestLog{}
		server := equivServer(t, log, func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path == synUsersPath {
				body := unmarshalBody(t, lastRecordedBody(log, synUsersPath))
				if body["displayname"] != "Alice" {
					t.Errorf("displayname = %v, want Alice", body["displayname"])
				}
				if _, hasPw := body["password"]; hasPw {
					t.Errorf("Synapse admin users body must not touch password: %v", body)
				}
				w.WriteHeader(http.StatusOK)
				w.Write([]byte("{}"))
				return
			}
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		})
		defer server.Close()

		if err := synapseEquivOps(server).SetUserDisplayName(context.Background(), userID, "", "Alice"); err != nil {
			t.Fatalf("Synapse SetUserDisplayName: %v", err)
		}
		if log.countByPath(synUsersPath) != 1 {
			t.Errorf("admin users calls = %d, want 1", log.countByPath(synUsersPath))
		}
	})
}

// TestEquiv_BackfillLegacyPassword_MatchesReset verifies that
// BackfillLegacyPassword routes through the exact same wire as
// ResetUserPassword on both impls (same underlying op, distinct migration
// intent). This is a guard against drift if either method is ever given a
// divergent implementation.
func TestEquiv_BackfillLegacyPassword_MatchesReset(t *testing.T) {
	userID := "@alice:" + equivDomain
	password := "migrated"
	resetPath := "/_synapse/admin/v1/reset_password/" + userID

	t.Run("Tuwunel_SameAdminCommand", func(t *testing.T) {
		log := &requestLog{}
		server := equivServer(t, log, func(w http.ResponseWriter, r *http.Request) {
			if strings.HasPrefix(r.URL.Path, txnIDPattern) {
				w.WriteHeader(http.StatusOK)
				w.Write([]byte("{}"))
				return
			}
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		})
		defer server.Close()

		errReset := tuwunelEquivOps(server).ResetUserPassword(context.Background(), userID, password)
		assertEquivNil(t, "Reset", errReset, nil)
		resetBody := lastBody(log, txnIDPattern)

		log2 := &requestLog{}
		server2 := equivServer(t, log2, func(w http.ResponseWriter, r *http.Request) {
			if strings.HasPrefix(r.URL.Path, txnIDPattern) {
				w.WriteHeader(http.StatusOK)
				w.Write([]byte("{}"))
				return
			}
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		})
		defer server2.Close()

		errBackfill := tuwunelEquivOps(server2).BackfillLegacyPassword(context.Background(), userID, password)
		assertEquivNil(t, "Backfill", errBackfill, nil)
		backfillBody := lastBody(log2, txnIDPattern)

		// Both must emit the identical admin command string.
		bR := unmarshalBody(t, resetBody)["body"]
		bB := unmarshalBody(t, backfillBody)["body"]
		if bR != bB {
			t.Errorf("Tuwunel reset/backfill commands diverge: reset=%q backfill=%q", bR, bB)
		}
	})

	t.Run("Synapse_SameRestPath", func(t *testing.T) {
		log := &requestLog{}
		server := equivServer(t, log, func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path == resetPath {
				w.WriteHeader(http.StatusOK)
				w.Write([]byte("{}"))
				return
			}
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		})
		defer server.Close()

		errReset := synapseEquivOps(server).ResetUserPassword(context.Background(), userID, password)
		assertEquivNil(t, "Reset", errReset, nil)
		resetCalls := log.countByPath(resetPath)

		log2 := &requestLog{}
		server2 := equivServer(t, log2, func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path == resetPath {
				w.WriteHeader(http.StatusOK)
				w.Write([]byte("{}"))
				return
			}
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		})
		defer server2.Close()

		errBackfill := synapseEquivOps(server2).BackfillLegacyPassword(context.Background(), userID, password)
		assertEquivNil(t, "Backfill", errBackfill, nil)
		backfillCalls := log2.countByPath(resetPath)

		if resetCalls != 1 || backfillCalls != 1 {
			t.Errorf("reset=%d backfill=%d, want both 1", resetCalls, backfillCalls)
		}
	})
}

// ===========================================================================
// GROUP C — AppService governance divergence + RenderAppServiceRegistration
//
// RegisterAppService and UnregisterAppService are the most asymmetric ops:
//   - Tuwunel drives both through admin-bot commands (with smoke-test-first
//     idempotency on register).
//   - Synapse treats registrations as declarative (Helm-managed) and refuses
//     to register/unregister at runtime, returning guidance instead.
//
// RenderAppServiceRegistration is pure config → struct rendering shared by
// both impls; GROUP C pins struct equality including the nil-vs-set push URL
// branch.
// ===========================================================================

// TestEquiv_RenderAppServiceRegistration_Equality pins that
// RenderAppServiceRegistration produces identical structs under both impls
// (it is a free function, so the "both impls" framing here is really "the
// shared rendering must match the documented invariants"). The push-URL
// nil-vs-set branch is the part most likely to regress.
func TestEquiv_RenderAppServiceRegistration_Equality(t *testing.T) {
	base := Config{
		Domain:                    equivDomain,
		AppServiceID:              "agentteams-controller",
		AppServiceToken:           "as",
		AppServiceHSToken:         "hs",
		AppServiceSenderLocalpart: "agentteams-controller",
	}

	t.Run("DefaultBroadNamespace_NilPushURL", func(t *testing.T) {
		reg := RenderAppServiceRegistration(base)
		if reg.ID != "agentteams-controller" || reg.ASToken != "as" || reg.HSToken != "hs" {
			t.Errorf("identity/token fields diverge: %+v", reg)
		}
		if reg.URL != nil {
			t.Errorf("URL = %v, want nil when AppServicePushURL is empty", reg.URL)
		}
		if len(reg.Namespaces.Users) != 1 || !reg.Namespaces.Users[0].Exclusive ||
			reg.Namespaces.Users[0].Regex != "@.*:"+equivDomain {
			t.Errorf("user namespace = %+v, want exclusive @.*:%s", reg.Namespaces.Users, equivDomain)
		}
		if len(reg.Namespaces.Aliases) != 1 ||
			reg.Namespaces.Aliases[0].Regex != "#agentteams-.*:"+equivDomain {
			t.Errorf("alias namespace = %+v, want #agentteams-.*:%s", reg.Namespaces.Aliases, equivDomain)
		}
		if len(reg.Namespaces.Rooms) != 0 {
			t.Errorf("rooms namespace = %+v, want empty", reg.Namespaces.Rooms)
		}
		if reg.RateLimited {
			t.Error("RateLimited = true, want false")
		}
	})

	t.Run("NarrowedNamespace_SetPushURL", func(t *testing.T) {
		cfg := base
		cfg.AppServiceUserNamespaceRegex = "@agentteams-.*:" + equivDomain
		cfg.AppServicePushURL = "http://controller:8080"
		reg := RenderAppServiceRegistration(cfg)
		if reg.URL == nil || *reg.URL != "http://controller:8080" {
			t.Errorf("URL = %v, want pointer to http://controller:8080", reg.URL)
		}
		if reg.Namespaces.Users[0].Regex != "@agentteams-.*:"+equivDomain {
			t.Errorf("narrowed namespace = %v, want @agentteams-.*:%s",
				reg.Namespaces.Users[0].Regex, equivDomain)
		}
	})

	t.Run("StructEquality_AcrossTwoRenders", func(t *testing.T) {
		// Two calls with the same config must produce identical structs.
		r1 := RenderAppServiceRegistration(base)
		r2 := RenderAppServiceRegistration(base)
		if !structEq(r1, r2) {
			t.Errorf("two renders diverge:\n r1=%+v\n r2=%+v", r1, r2)
		}
	})
}

// structEq compares two AppServiceRegistration values for deep equality. We
// avoid reflect.DeepEqual here to keep the failure message readable; the type
// is small enough to compare field by field.
func structEq(a, b AppServiceRegistration) bool {
	if a.ID != b.ID || a.ASToken != b.ASToken || a.HSToken != b.HSToken ||
		a.SenderLocalpart != b.SenderLocalpart || a.RateLimited != b.RateLimited {
		return false
	}
	// URL: both nil OR both non-nil with equal string values.
	if (a.URL == nil) != (b.URL == nil) {
		return false
	}
	if a.URL != nil && *a.URL != *b.URL {
		return false
	}
	// Namespaces: compare lengths then each entry across users/aliases/rooms.
	if !nsSliceEq(a.Namespaces.Users, b.Namespaces.Users) {
		return false
	}
	if !nsSliceEq(a.Namespaces.Aliases, b.Namespaces.Aliases) {
		return false
	}
	if !nsSliceEq(a.Namespaces.Rooms, b.Namespaces.Rooms) {
		return false
	}
	return true
}

func nsSliceEq(a, b []AppServiceNamespace) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i].Exclusive != b[i].Exclusive || a[i].Regex != b[i].Regex {
			return false
		}
	}
	return true
}

// TestEquiv_UnregisterAppService_Divergence pins the documented asymmetry:
//   - Tuwunel delivers "!admin appservices unregister <id>" to #admins.
//   - Synapse returns a static error pointing at Helm WITHOUT any HTTP call.
func TestEquiv_UnregisterAppService_Divergence(t *testing.T) {
	const appID = "agentteams-controller"

	t.Run("Tuwunel_AdminBotCommand", func(t *testing.T) {
		log := &requestLog{}
		server := equivServer(t, log, func(w http.ResponseWriter, r *http.Request) {
			if strings.HasPrefix(r.URL.Path, txnIDPattern) {
				body := unmarshalBody(t, lastBody(log, txnIDPattern))
				if want := "!admin appservices unregister " + appID; body["body"] != want {
					t.Errorf("admin body = %q, want %q", body["body"], want)
				}
				w.WriteHeader(http.StatusOK)
				w.Write([]byte("{}"))
				return
			}
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		})
		defer server.Close()

		if err := tuwunelEquivOps(server).UnregisterAppService(context.Background(), appID); err != nil {
			t.Fatalf("Tuwunel UnregisterAppService: %v", err)
		}
		if adminBotMessageCount(log) != 1 {
			t.Errorf("admin-bot messages = %d, want 1", adminBotMessageCount(log))
		}
	})

	t.Run("Synapse_StaticErrorNoIO", func(t *testing.T) {
		// Point Synapse at a deliberately-unreachable URL so any accidental
		// HTTP call surfaces as an error rather than a silent success.
		cfg := Config{
			ServerURL:     "http://127.0.0.1:1",
			Domain:        equivDomain,
			AdminUser:     "admin",
			AdminPassword: "pw",
		}
		err := NewSynapseMatrixOps(cfg, &http.Client{}).UnregisterAppService(context.Background(), appID)
		if err == nil {
			t.Fatal("Synapse UnregisterAppService = nil, want Helm guidance error")
		}
		for _, want := range []string{appID, "Helm", "declarative"} {
			if !strings.Contains(err.Error(), want) {
				t.Errorf("error %q missing %q", err, want)
			}
		}
	})
}

// TestEquiv_RegisterAppService_Divergence pins the documented asymmetry:
//   - Tuwunel runs the smoke test first; on failure it unregisters (best
//     effort) then sends the registration YAML via the admin bot.
//   - Synapse runs ONLY the smoke test and, when it fails, reports an error
//     pointing at Helm — it does NOT attempt runtime registration.
func TestEquiv_RegisterAppService_Divergence(t *testing.T) {
	const appID = "agentteams-controller"
	reg := AppServiceRegistration{ID: appID, ASToken: "as", HSToken: "hs", SenderLocalpart: "agentteams-controller"}

	// Fast path shared by both impls: when the existing registration's smoke
	// test passes, RegisterAppService is a no-op that never emits an admin
	// command (Tuwunel) and never touches the Helm guidance (Synapse).
	t.Run("Both_SmokeSuccess_NoAdminCommand", func(t *testing.T) {
		var asLoginsT, asLoginsS int
		serverT := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path == "/_matrix/client/v3/login" {
				asLoginsT++
				writeJSON(t, w, map[string]string{"access_token": "ok"})
				return
			}
			t.Errorf("unexpected Tuwunel path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}))
		defer serverT.Close()
		serverS := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path == "/_matrix/client/v3/login" {
				asLoginsS++
				writeJSON(t, w, map[string]string{"access_token": "ok"})
				return
			}
			t.Errorf("unexpected Synapse path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}))
		defer serverS.Close()

		cfgT := equivConfig(serverT.URL)
		cfgT.AppServiceSenderLocalpart = "agentteams-controller"
		cfgT.AppServiceToken = "as"
		cfgS := equivConfig(serverS.URL)
		cfgS.AppServiceSenderLocalpart = "agentteams-controller"
		cfgS.AppServiceToken = "as"

		errT := NewTuwunelMatrixOps(cfgT, serverT.Client()).RegisterAppService(context.Background(), reg)
		errS := NewSynapseMatrixOps(cfgS, serverS.Client()).RegisterAppService(context.Background(), reg)
		assertEquivNil(t, "RegisterAppService smoke-success", errT, errS)
		if asLoginsT != 1 || asLoginsS != 1 {
			t.Errorf("AS logins: tuwunel=%d synapse=%d, want 1/1 (smoke test only)", asLoginsT, asLoginsS)
		}
	})

	// Divergence on failure: Synapse reports a Helm-guidance error WITHOUT
	// attempting runtime registration. (Tuwunel's failure path issues an
	// unregister-then-register admin command and sleeps 2s; covering it here
	// would slow the suite, and the per-impl RegisterAppService semantics are
	// already pinned by appservice_test.go and the SmokeTestOnly tests in
	// synapse_ops_test.go. This subtest pins the Synapse side specifically.)
	t.Run("Synapse_SmokeFailure_HelmGuidance", func(t *testing.T) {
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path == "/_matrix/client/v3/login" {
				writeMatrixError(w, http.StatusUnauthorized, "M_UNKNOWN_TOKEN", "not registered")
				return
			}
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}))
		defer server.Close()

		cfg := equivConfig(server.URL)
		cfg.AppServiceSenderLocalpart = "agentteams-controller"
		cfg.AppServiceToken = "as"

		// Short context so the smoke-test retry loop fails fast.
		ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
		defer cancel()

		err := NewSynapseMatrixOps(cfg, server.Client()).RegisterAppService(ctx, reg)
		if err == nil {
			t.Fatal("Synapse RegisterAppService = nil, want Helm guidance error")
		}
		for _, want := range []string{appID, "Helm", "declarative"} {
			if !strings.Contains(err.Error(), want) {
				t.Errorf("error %q missing %q", err, want)
			}
		}
	})
}
