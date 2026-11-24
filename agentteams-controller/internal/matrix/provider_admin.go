package matrix

import (
	"context"
	"fmt"
)

// ProviderAdminOps is the provider-neutral surface for privileged admin
// operations that the business layer must perform but that are not part of the
// Matrix CS API:
//
//   - DeleteRoom disposes of a room (purge / delete-room).
//   - ForceLeaveRoom removes a user from a room without requiring that user's
//     own access token (used by Human deletion when no token is available).
//   - DeactivateUser disables a Matrix account (Human offboarding).
//
// Both concrete clients implement it:
//
//   - TuwunelClient keeps the historical admin-bot path ("!admin ..." commands
//     delivered to the #admins room); formatting the commands is now internal
//     to the client instead of living in the business layer.
//   - SynapseClient uses the Synapse admin REST API
//     (DELETE /_synapse/admin/v2/rooms/{id}, POST /_synapse/admin/v1/deactivate/{id},
//     and join/make_room_admin + CS kick for force-leave).
//
// Business code asks for the capability (type assertion on matrix.Client) and
// falls back to AdminCommand for implementations that do not provide it (for
// example simple test fakes).
type ProviderAdminOps interface {
	// DeleteRoom disposes of roomID. Fire-and-forget: the homeserver may
	// process the deletion asynchronously.
	DeleteRoom(ctx context.Context, roomID string) error
	// ForceLeaveRoom removes userID from roomID without the user's own token.
	// Idempotent: nil when the user is not currently in the room.
	ForceLeaveRoom(ctx context.Context, userID, roomID string) error
	// DeactivateUser disables the Matrix account userID.
	DeactivateUser(ctx context.Context, userID string) error
}

var (
	_ ProviderAdminOps = (*TuwunelClient)(nil)
	_ ProviderAdminOps = (*SynapseClient)(nil)
)

// DeleteRoom sends the Tuwunel "!admin rooms delete-room" command to the admin
// bot. Tuwunel processes it asynchronously.
func (c *TuwunelClient) DeleteRoom(ctx context.Context, roomID string) error {
	if roomID == "" {
		return nil
	}
	return c.AdminCommand(ctx, fmt.Sprintf("!admin rooms delete-room %s", roomID))
}

// ForceLeaveRoom sends the Tuwunel "!admin users force-leave-room" command to
// the admin bot. Used by the Human delete flow where the controller no longer
// holds a valid user token.
func (c *TuwunelClient) ForceLeaveRoom(ctx context.Context, userID, roomID string) error {
	if userID == "" || roomID == "" {
		return nil
	}
	return c.AdminCommand(ctx, fmt.Sprintf("!admin users force-leave-room %s %s", userID, roomID))
}

// DeactivateUser sends the Tuwunel "!admin users deactivate" command to the
// admin bot and drops any cached login token for the account.
func (c *TuwunelClient) DeactivateUser(ctx context.Context, userID string) error {
	if userID == "" {
		return nil
	}
	if err := c.AdminCommand(ctx, fmt.Sprintf("!admin users deactivate %s", userID)); err != nil {
		return err
	}
	c.InvalidateUserToken(userID)
	return nil
}
