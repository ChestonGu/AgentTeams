package matrix

import (
	"fmt"
	"net/http"
	"strings"
)

// NewClient returns the Matrix Client implementation selected by provider.
//
//   - "" or "tuwunel" (default): TuwunelClient — admin ops via the Tuwunel
//     admin bot ("!admin ..."), the zero-regression reference implementation.
//   - "synapse": SynapseClient — admin ops via the Synapse admin REST API
//     (/_synapse/admin/v1/*) and the standard Matrix CS API; AppService
//     registrations are declarative (file-based) instead of runtime
//     admin-bot commands.
//
// The provider name is compared case-insensitively. The same selection is
// validated at config load time (AGENTTEAMS_MATRIX_PROVIDER), so this factory
// is the single, unit-testable source of truth for the provider →
// implementation mapping used by the app wiring and HTTP handlers.
func NewClient(provider string, cfg Config, httpClient *http.Client) (Client, error) {
	switch strings.ToLower(strings.TrimSpace(provider)) {
	case "", "tuwunel":
		return NewTuwunelClient(cfg, httpClient), nil
	case "synapse":
		return NewSynapseClient(cfg, httpClient), nil
	default:
		return nil, fmt.Errorf("unknown matrix provider %q: valid values are \"tuwunel\" (default) or \"synapse\"", provider)
	}
}
