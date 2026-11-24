package matrix

import (
	"context"
	"fmt"
	"net/http"
	"net/url"
	"strings"
)

// This file holds the Synapse-specific admin REST helpers used by
// SynapseClient: the generic admin call primitive, the membership/power
// recovery endpoints (join + make_room_admin), password reset, and the
// classification of sender-side CS operation failures into a recovery strategy.
//
// All calls authenticate with the cached admin access token
// (ensureAdminToken) and require the admin account to be a Synapse server
// admin; otherwise Synapse answers 403.

// synAdminCall issues a Synapse admin REST request with the cached admin access
// token and treats any 2xx as success. respOut, when non-nil, receives the
// decoded JSON response body.
func (s *SynapseClient) synAdminCall(ctx context.Context, method, path string, body interface{}, respOut interface{}) error {
	token, err := s.ensureAdminToken(ctx)
	if err != nil {
		return fmt.Errorf("synapse admin %s %s: %w", method, path, err)
	}
	statusCode, respBody, err := s.doJSON(ctx, method, path, token, body, respOut)
	if err != nil {
		return fmt.Errorf("synapse admin %s %s: %w", method, path, err)
	}
	if statusCode < 200 || statusCode >= 300 {
		return fmt.Errorf("synapse admin %s %s: HTTP %d: %s", method, path, statusCode, truncate(respBody, 500))
	}
	return nil
}

// synResetPassword sets a user's password through the Synapse admin API.
// logoutDevices controls whether existing access tokens are revoked.
func (s *SynapseClient) synResetPassword(ctx context.Context, userID, password string, logoutDevices bool) error {
	path := "/_synapse/admin/v1/reset_password/" + url.PathEscape(userID)
	body := map[string]interface{}{
		"new_password":   password,
		"logout_devices": logoutDevices,
	}
	if err := s.synAdminCall(ctx, http.MethodPost, path, body, nil); err != nil {
		return fmt.Errorf("synapse reset password for %s: %w", userID, err)
	}
	return nil
}

// MakeRoomAdmin raises userID to the room's highest current admin power level
// via the Synapse admin API. It is the recovery path for in-room CS operations
// (invite/kick/state) rejected because the operator lacks power. Note that the
// API only invites a non-joined user instead of force-joining them; callers
// that need actual membership must pair it with ForceJoinRoom.
//
//	POST /_synapse/admin/v1/rooms/{roomID}/make_room_admin
//	body: {"user_id": "<userID>"}
func (s *SynapseClient) MakeRoomAdmin(ctx context.Context, roomID, userID string) error {
	path := "/_synapse/admin/v1/rooms/" + url.PathEscape(roomID) + "/make_room_admin"
	if err := s.synAdminCall(ctx, http.MethodPost, path, map[string]string{"user_id": userID}, nil); err != nil {
		return fmt.Errorf("synapse make_room_admin %s in %s: %w", userID, roomID, err)
	}
	return nil
}

// ForceJoinRoom joins userID into roomID through the Synapse admin API, which
// bypasses join rules (auto-invite followed by an immediate join for
// non-public rooms). Idempotent: an already-joined user returns 200.
//
//	POST /_synapse/admin/v1/join/{roomID}
//	body: {"user_id": "<userID>"}
func (s *SynapseClient) ForceJoinRoom(ctx context.Context, roomID, userID string) error {
	path := "/_synapse/admin/v1/join/" + url.PathEscape(roomID)
	if err := s.synAdminCall(ctx, http.MethodPost, path, map[string]string{"user_id": userID}, nil); err != nil {
		return fmt.Errorf("synapse force join %s into %s: %w", userID, roomID, err)
	}
	return nil
}

// synapseSenderRecovery classifies a failed in-room CS operation's sender-side
// rejection into the Synapse-native recovery that addresses it.
type synapseSenderRecovery int

const (
	// synapseRecoveryNone: no recovery applies — return the error unchanged.
	synapseRecoveryNone synapseSenderRecovery = iota
	// synapseRecoveryJoin: the sender is not joined to the room. Recover by
	// force-joining the sender via POST /_synapse/admin/v1/join/{roomID}.
	synapseRecoveryJoin
	// synapseRecoveryPower: the sender is joined but lacks the required power
	// level. Recover by raising the sender via make_room_admin.
	synapseRecoveryPower
)

func (r synapseSenderRecovery) String() string {
	switch r {
	case synapseRecoveryJoin:
		return "force-join"
	case synapseRecoveryPower:
		return "make_room_admin"
	default:
		return "none"
	}
}

// classifySynapseSenderError maps a failed CS operation's error to the
// Synapse-native recovery that addresses it. Errors that are nil, not
// M_FORBIDDEN, or not a sender-membership/power rejection map to
// synapseRecoveryNone. Idempotent already-joined/already-invited cases return
// nil inside the CS client before an error reaches here.
func classifySynapseSenderError(err error) synapseSenderRecovery {
	if err == nil {
		return synapseRecoveryNone
	}
	msg := strings.ToLower(err.Error())
	if !strings.Contains(msg, "m_forbidden") {
		return synapseRecoveryNone
	}
	// Sender not joined to the room. The target-not-in-room idempotent string
	// ("The target user is not in the room") is deliberately not matched here:
	// KickFromRoom already treats the target-side 404/forbidden as success.
	if strings.Contains(msg, "not in room") {
		return synapseRecoveryJoin
	}
	// Insufficient power level (invite/post/state/kick/redact checks).
	if strings.Contains(msg, "permission to invite") ||
		strings.Contains(msg, "permission to post") ||
		strings.Contains(msg, "permission to send") ||
		strings.Contains(msg, "cannot kick user") ||
		strings.Contains(msg, "cannot unban user") ||
		strings.Contains(msg, "not have permission") {
		return synapseRecoveryPower
	}
	return synapseRecoveryNone
}

// recoverSenderOp recovers a CS operation that failed with a sender-side
// rejection and retries it, composing Synapse's own admin capabilities:
//   - synapseRecoveryJoin: force-join the sender via POST /_synapse/admin/v1/join.
//   - synapseRecoveryPower: make_room_admin the sender, then retry. If the
//     sender was raised but still is not joined (make_room_admin only invites
//     non-joined users), a force-join is attempted before the retry.
//
// Any other error is returned unchanged. op names the failed operation for
// error wrapping (e.g. "invite @alice:d to").
func (s *SynapseClient) recoverSenderOp(ctx context.Context, roomID, op, senderID string, retry func() error, err error) error {
	recovery := classifySynapseSenderError(err)
	if recovery == synapseRecoveryNone {
		return err
	}
	s.logSynapseRecovery(ctx, roomID, op, recovery, err)

	if recovery == synapseRecoveryJoin {
		if joinErr := s.ForceJoinRoom(ctx, roomID, senderID); joinErr != nil {
			return fmt.Errorf("%s %s failed (%v) and force-join failed: %w", op, roomID, err, joinErr)
		}
		retryErr := retry()
		if retryErr == nil {
			return nil
		}
		if classifySynapseSenderError(retryErr) != synapseRecoveryPower {
			return retryErr
		}
		err = retryErr
		recovery = synapseRecoveryPower
	}

	if recovery != synapseRecoveryPower {
		return err
	}
	if adminErr := s.MakeRoomAdmin(ctx, roomID, senderID); adminErr != nil {
		return fmt.Errorf("%s %s failed (%v) and make_room_admin failed: %w", op, roomID, err, adminErr)
	}
	// make_room_admin only invites a non-joined sender; make sure the sender is
	// actually in the room before retrying the CS operation.
	if joinErr := s.ForceJoinRoom(ctx, roomID, senderID); joinErr != nil {
		// Some CS operations don't require the sender to be joined (e.g. a
		// public-room join); keep going and let the retry decide.
		s.logSynapseRecovery(ctx, roomID, op+" (post-make_room_admin join)", synapseRecoveryJoin, joinErr)
	}
	return retry()
}

// synDemoteAndKick lowers the target's power level in roomID to 0 and then
// retries the kick. It handles the one case the join/make_room_admin recovery
// cannot: an equal-power target (e.g. the Manager is PL 100 in a worker's
// personal room and the controller admin is also PL 100). Room auth requires
// the target's PL to be strictly below the kicker's, so the admin first edits
// m.room.power_levels (PL 100 can edit power levels) and kicks afterwards.
// The power level is restored when the kick still fails, so a rejected kick
// never degrades the target's room privileges.
func (s *SynapseClient) synDemoteAndKick(ctx context.Context, roomID, userID string, kick func() error) error {
	token, err := s.ensureAdminToken(ctx)
	if err != nil {
		return fmt.Errorf("synapse demote-and-kick %s in %s: %w", userID, roomID, err)
	}
	state, err := s.GetRoomState(ctx, roomID, "m.room.power_levels", "", token)
	if err != nil {
		return fmt.Errorf("synapse demote-and-kick %s in %s: read power levels: %w", userID, roomID, err)
	}
	if state == nil {
		return fmt.Errorf("synapse demote-and-kick %s in %s: room has no m.room.power_levels", userID, roomID)
	}
	users, _ := state["users"].(map[string]interface{})
	if users == nil {
		return fmt.Errorf("synapse demote-and-kick %s in %s: power levels carry no users map", userID, roomID)
	}
	original, present := users[userID]
	if !present {
		return kick()
	}

	demoted := make(map[string]interface{}, len(users))
	for k, v := range users {
		demoted[k] = normalizeJSONNumbers(v)
	}
	demoted[userID] = int64(0)
	newState := make(map[string]interface{}, len(state))
	for k, v := range state {
		if k == "users" {
			continue
		}
		newState[k] = normalizeJSONNumbers(v)
	}
	newState["users"] = demoted

	if err := s.SetRoomState(ctx, roomID, "m.room.power_levels", "", newState, token); err != nil {
		return fmt.Errorf("synapse demote-and-kick %s in %s: lower power level: %w", userID, roomID, err)
	}
	if kickErr := kick(); kickErr != nil {
		// Restore the original power level: the kick was rejected for another
		// reason, so the demotion must not stick.
		restored := make(map[string]interface{}, len(users))
		for k, v := range users {
			restored[k] = normalizeJSONNumbers(v)
		}
		restored[userID] = normalizeJSONNumbers(original)
		restoreState := make(map[string]interface{}, len(state))
		for k, v := range state {
			if k == "users" {
				continue
			}
			restoreState[k] = normalizeJSONNumbers(v)
		}
		restoreState["users"] = restored
		if restoreErr := s.SetRoomState(ctx, roomID, "m.room.power_levels", "", restoreState, token); restoreErr != nil {
			s.logSynapseRecovery(ctx, roomID, "restore power level of "+userID, synapseRecoveryPower, restoreErr)
		}
		return fmt.Errorf("synapse demote-and-kick %s in %s: %w", userID, roomID, kickErr)
	}
	return nil
}

// normalizeJSONNumbers converts decoded JSON floats that carry integral values
// (e.g. 100.0 from a JSON state event) into int64. Synapse validates power
// level values as integers and rejects floats when they are written back.
func normalizeJSONNumbers(v interface{}) interface{} {
	switch value := v.(type) {
	case float64:
		if value == float64(int64(value)) {
			return int64(value)
		}
		return value
	case map[string]interface{}:
		out := make(map[string]interface{}, len(value))
		for k, item := range value {
			out[k] = normalizeJSONNumbers(item)
		}
		return out
	case []interface{}:
		out := make([]interface{}, len(value))
		for i, item := range value {
			out[i] = normalizeJSONNumbers(item)
		}
		return out
	default:
		return v
	}
}
