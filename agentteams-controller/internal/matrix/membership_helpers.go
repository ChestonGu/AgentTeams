package matrix

import (
	"context"
	"fmt"
	"strings"

	"sigs.k8s.io/controller-runtime/pkg/log"
)

// membershipClient is the protocol-level surface the shared Phase 2 helpers
// need. Both *TuwunelClient and the Client interface satisfy it, so the same
// convergence/join/leave/force-leave logic is shared verbatim between
// TuwunelMatrixOps, SynapseMatrixOps and the LegacyClientOps bridge.
type membershipClient interface {
	UserID(localpart string) string
	ListRoomMembers(ctx context.Context, roomID string) ([]RoomMember, error)
	ListRoomMembersWithToken(ctx context.Context, roomID, userToken string) ([]RoomMember, error)
	InviteToRoomWithToken(ctx context.Context, roomID, userID, inviterToken string) error
	KickFromRoomWithToken(ctx context.Context, roomID, userID, reason, kickerToken string) error
	JoinRoom(ctx context.Context, roomID, userToken string) error
	LeaveRoom(ctx context.Context, roomID, userToken string) error
	ListJoinedRooms(ctx context.Context, userToken string) ([]string, error)
	SetRoomName(ctx context.Context, roomID, name, userToken string) error
	DeleteRoomAlias(ctx context.Context, alias string) error
	ResolveRoomAlias(ctx context.Context, alias string) (string, bool, error)
}

// memberOps is the MatrixOps-level mutation surface reconcileMembersImpl needs
// for the non-actor (admin) path. Both TuwunelMatrixOps and SynapseMatrixOps
// satisfy it, so the escalation inside reconcileMembersImpl stays
// provider-agnostic (Tuwunel: admin-bot force-leave; Synapse: make_room_admin).
type memberOps interface {
	AddMember(ctx context.Context, roomID, userID string) error
	RemoveMember(ctx context.Context, roomID, userID, reason string) error
}

// memberTokenFor resolves the access token a member operation should act as:
// the member's own ActorToken when set, else the provider's admin identity.
// adminToken must return a fresh/valid admin access token for the homeserver.
func memberTokenFor(ctx context.Context, member MemberSpec, adminToken func(context.Context) (string, error)) (string, error) {
	if member.ActorToken != "" {
		return member.ActorToken, nil
	}
	return adminToken(ctx)
}

// reconcileMembersImpl converges roomID's membership to the desired set. It is
// the shared core behind MatrixOps.ReconcileMembers for every implementation.
//
// Behavior (deliberately identical to the pre-abstraction service layer's
// ReconcileRoomMembershipWithActorToken):
//   - When any desired spec carries an ActorToken, that token drives listing,
//     invites and kicks (the pre-abstraction layer used one actor token for the
//     whole room); a kick that fails with a power/sender error escalates once
//     via ops.RemoveMember.
//   - Otherwise the admin identity drives listing/invites/kicks.
//   - The homeserver admin user is never removed implicitly.
//   - Per-user errors are logged and collected; the first error is returned
//     after processing every user (best-effort).
func reconcileMembersImpl(ctx context.Context, ops memberOps, client membershipClient, adminUserLocalpart, roomID string, desired []MemberSpec) error {
	logger := log.FromContext(ctx)

	actorToken := ""
	for _, m := range desired {
		if m.ActorToken != "" {
			actorToken = m.ActorToken
			break
		}
	}

	desiredSet := make(map[string]struct{}, len(desired))
	for _, m := range desired {
		if m.UserID != "" {
			desiredSet[m.UserID] = struct{}{}
		}
	}

	var current []RoomMember
	var err error
	if actorToken != "" {
		current, err = client.ListRoomMembersWithToken(ctx, roomID, actorToken)
	} else {
		current, err = client.ListRoomMembers(ctx, roomID)
	}
	if err != nil {
		return fmt.Errorf("list members of %s: %w", roomID, err)
	}

	currentSet := make(map[string]struct{}, len(current))
	for _, m := range current {
		currentSet[m.UserID] = struct{}{}
	}

	adminID := client.UserID(adminUserLocalpart)
	var firstErr error

	// Add members in `desired` but not currently present.
	for _, m := range desired {
		u := m.UserID
		if u == "" {
			continue
		}
		if _, ok := currentSet[u]; ok {
			continue
		}
		var err error
		if actorToken != "" {
			logger.Info("inviting user to room with joined member token", "room", roomID, "user", u)
			err = client.InviteToRoomWithToken(ctx, roomID, u, actorToken)
		} else {
			err = ops.AddMember(ctx, roomID, u)
		}
		if err != nil {
			logger.Error(err, "failed to invite user to room", "room", roomID, "user", u)
			if firstErr == nil {
				firstErr = err
			}
		}
	}

	// Remove members currently present but not in `desired`. Leave the
	// homeserver admin alone even if not desired: admin owns power level 100
	// and some rooms (e.g. Manager Admin DM) expect it implicitly.
	for _, m := range current {
		if _, ok := desiredSet[m.UserID]; ok {
			continue
		}
		if m.UserID == adminID {
			continue
		}
		logger.Info("room member not desired; attempting removal",
			"room", roomID, "user", m.UserID, "membership", m.Membership)
		var err error
		if actorToken != "" {
			logger.Info("kicking user from room with joined member token", "room", roomID, "user", m.UserID)
			err = client.KickFromRoomWithToken(ctx, roomID, m.UserID, "removed from desired member set", actorToken)
			// Escalate a power/sender failure once via the ops layer, which
			// knows the provider-specific fallback (Tuwunel admin bot
			// force-leave; Synapse make_room_admin + retry).
			if err != nil && shouldForceLeaveAfterKickError(err) {
				logger.Info("kick with actor token failed; escalating to admin RemoveMember",
					"room", roomID, "user", m.UserID)
				err = ops.RemoveMember(ctx, roomID, m.UserID, "removed from desired member set")
			}
		} else {
			err = ops.RemoveMember(ctx, roomID, m.UserID, "removed from desired member set")
		}
		if err != nil {
			logger.Error(err, "failed to kick user from room", "room", roomID, "user", m.UserID)
			if firstErr == nil {
				firstErr = err
			}
		}
	}

	return firstErr
}

// joinRoomForMember joins roomID as the user described by member. Empty
// ActorToken falls back to the admin identity.
func joinRoomForMember(ctx context.Context, client membershipClient, roomID string, member MemberSpec, adminToken func(context.Context) (string, error)) error {
	token, err := memberTokenFor(ctx, member, adminToken)
	if err != nil {
		return fmt.Errorf("join room %s: %w", roomID, err)
	}
	return client.JoinRoom(ctx, roomID, token)
}

// leaveRoomForMember leaves roomID as the user described by member. Empty
// ActorToken falls back to the admin identity.
func leaveRoomForMember(ctx context.Context, client membershipClient, roomID string, member MemberSpec, adminToken func(context.Context) (string, error)) error {
	token, err := memberTokenFor(ctx, member, adminToken)
	if err != nil {
		return fmt.Errorf("leave room %s: %w", roomID, err)
	}
	return client.LeaveRoom(ctx, roomID, token)
}

// forceLeaveAllRoomsForMember makes the user described by member leave every
// room they are currently joined to. Best-effort: errors leaving individual
// rooms are logged, not returned. Empty ActorToken falls back to the admin
// identity.
func forceLeaveAllRoomsForMember(ctx context.Context, client membershipClient, member MemberSpec, adminToken func(context.Context) (string, error)) error {
	logger := log.FromContext(ctx)
	token, err := memberTokenFor(ctx, member, adminToken)
	if err != nil {
		return fmt.Errorf("list joined rooms: %w", err)
	}
	rooms, err := client.ListJoinedRooms(ctx, token)
	if err != nil {
		return fmt.Errorf("list joined rooms: %w", err)
	}
	for _, roomID := range rooms {
		if err := client.LeaveRoom(ctx, roomID, token); err != nil {
			logger.Error(err, "leave room (best-effort)", "roomID", roomID)
		}
	}
	return nil
}

// === Phase 3 shared helpers ===

// listRoomMembersForMember lists roomID's members as the user described by
// member: member.ActorToken when set, else the admin identity.
func listRoomMembersForMember(ctx context.Context, client membershipClient, roomID string, member MemberSpec) ([]RoomMember, error) {
	if member.ActorToken != "" {
		return client.ListRoomMembersWithToken(ctx, roomID, member.ActorToken)
	}
	return client.ListRoomMembers(ctx, roomID)
}

// isUserInRoomForMember reports whether userID is currently joined
// (membership "join") to roomID. Reads via the admin identity (bypasses
// in-room checks).
func isUserInRoomForMember(ctx context.Context, client membershipClient, roomID, userID string) (bool, error) {
	members, err := client.ListRoomMembers(ctx, roomID)
	if err != nil {
		return false, err
	}
	for _, m := range members {
		if m.UserID == userID && m.Membership == "join" {
			return true, nil
		}
	}
	return false, nil
}

// isManagerJoinedDMForMember reports whether the Manager's Matrix user
// (localpart "manager") is currently `join`ed to roomID. Reads via the admin
// identity (bypasses in-room checks).
func isManagerJoinedDMForMember(ctx context.Context, client membershipClient, roomID string) (bool, error) {
	managerID := client.UserID("manager")
	members, err := client.ListRoomMembers(ctx, roomID)
	if err != nil {
		return false, fmt.Errorf("list members of %s: %w", roomID, err)
	}
	for _, m := range members {
		if m.UserID == managerID && m.Membership == "join" {
			return true, nil
		}
	}
	return false, nil
}

// isMatrixConnError reports whether err is a transport-level failure
// (connection refused, DNS error, dial timeout, EOF) as opposed to an
// HTTP-level response. Used by HealthCheck to distinguish "homeserver is
// down" (transport error) from "homeserver is up but rejected the request"
// (auth/HTTP error → nil).
func isMatrixConnError(err error) bool {
	if err == nil {
		return false
	}
	msg := strings.ToLower(err.Error())
	for _, sub := range []string{"connection refused", "no such host", "dial tcp", "i/o timeout", "eof"} {
		if strings.Contains(msg, sub) {
			return true
		}
	}
	return false
}
