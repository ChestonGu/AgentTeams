package config

import (
	"strings"
	"testing"
)

// TestMatrixProviderDefaultsToTuwunel verifies that AGENTTEAMS_MATRIX_PROVIDER
// unset → MatrixProvider="tuwunel" and UsesSynapse()=false.
//
// Note: t.Setenv with "" leaves the env set to empty string, which
// envOrDefault treats as "use default" → "tuwunel". This matches the
// production startup behavior when the env is genuinely unset.
func TestMatrixProviderDefaultsToTuwunel(t *testing.T) {
	t.Setenv("AGENTTEAMS_MATRIX_PROVIDER", "")
	cfg := LoadConfig()
	if cfg.MatrixProvider != "tuwunel" {
		t.Fatalf("MatrixProvider = %q, want %q", cfg.MatrixProvider, "tuwunel")
	}
	if cfg.UsesSynapse() {
		t.Fatalf("UsesSynapse() = true, want false when provider unset")
	}
	if cfg.MatrixConfig().Provider != "tuwunel" {
		t.Fatalf("MatrixConfig().Provider = %q, want %q", cfg.MatrixConfig().Provider, "tuwunel")
	}
}

// TestMatrixProviderSynapse verifies that AGENTTEAMS_MATRIX_PROVIDER=synapse
// → MatrixProvider="synapse" and UsesSynapse()=true.
func TestMatrixProviderSynapse(t *testing.T) {
	t.Setenv("AGENTTEAMS_MATRIX_PROVIDER", "synapse")
	cfg := LoadConfig()
	if cfg.MatrixProvider != "synapse" {
		t.Fatalf("MatrixProvider = %q, want %q", cfg.MatrixProvider, "synapse")
	}
	if !cfg.UsesSynapse() {
		t.Fatalf("UsesSynapse() = false, want true")
	}
	if cfg.MatrixConfig().Provider != "synapse" {
		t.Fatalf("MatrixConfig().Provider = %q, want %q", cfg.MatrixConfig().Provider, "synapse")
	}
}

// TestMatrixProviderCaseInsensitive verifies the value is lowercased before
// comparison (so "SYNAPSE" / "Synapse" work too).
func TestMatrixProviderCaseInsensitive(t *testing.T) {
	t.Setenv("AGENTTEAMS_MATRIX_PROVIDER", "SYNAPSE")
	cfg := LoadConfig()
	if cfg.MatrixProvider != "synapse" {
		t.Fatalf("MatrixProvider = %q, want %q (should be lowercased)", cfg.MatrixProvider, "synapse")
	}
	if !cfg.UsesSynapse() {
		t.Fatalf("UsesSynapse() = false, want true")
	}
}

// TestMatrixProviderUnknownPanics verifies that an unrecognized provider value
// fails startup with a panic mentioning valid values.
func TestMatrixProviderUnknownPanics(t *testing.T) {
	t.Setenv("AGENTTEAMS_MATRIX_PROVIDER", "dendrite")
	defer func() {
		r := recover()
		if r == nil {
			t.Fatalf("LoadConfig did not panic for provider=dendrite")
		}
		msg, ok := r.(string)
		if !ok {
			t.Fatalf("panic value not string: %v", r)
		}
		if !strings.Contains(msg, "AGENTTEAMS_MATRIX_PROVIDER") || !strings.Contains(msg, "dendrite") {
			t.Fatalf("panic message does not mention env var and bad value: %q", msg)
		}
		if !strings.Contains(msg, "tuwunel") || !strings.Contains(msg, "synapse") {
			t.Fatalf("panic message does not list valid values: %q", msg)
		}
	}()
	LoadConfig()
	t.Fatal("LoadConfig should have panicked")
}
