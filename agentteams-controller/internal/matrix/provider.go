package matrix

import (
	"fmt"
	"net/http"
	"strings"
)

// NewOps returns the MatrixOps implementation selected by provider.
//
//   - "" or "tuwunel" (default): TuwunelMatrixOps — admin ops via the
//     Tuwunel admin bot ("!admin ..."), the zero-regression reference
//     implementation.
//   - "synapse": SynapseMatrixOps — admin ops via the Synapse admin REST API
//     with make_room_admin fallback for in-room CS operations.
//
// Any other value is an error. The provider name is compared
// case-insensitively. The same config validation is enforced at load time in
// the config package (AGENTTEAMS_MATRIX_PROVIDER), so this factory is the
// single, unit-testable source of truth for the provider → implementation
// mapping.
func NewOps(provider string, cfg Config, httpClient *http.Client) (MatrixOps, error) {
	switch strings.ToLower(strings.TrimSpace(provider)) {
	case "", "tuwunel":
		return NewTuwunelMatrixOps(cfg, httpClient), nil
	case "synapse":
		return NewSynapseMatrixOps(cfg, httpClient), nil
	default:
		return nil, fmt.Errorf("unknown matrix provider %q: valid values are \"tuwunel\" (default) or \"synapse\"", provider)
	}
}
