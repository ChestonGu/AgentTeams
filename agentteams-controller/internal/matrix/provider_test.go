package matrix

import (
	"net/http"
	"testing"
)

// TestNewOps_DefaultsToTuwunel verifies that an empty provider selects
// TuwunelMatrixOps (the default).
func TestNewOps_DefaultsToTuwunel(t *testing.T) {
	ops, err := NewOps("", Config{}, nil)
	if err != nil {
		t.Fatalf("NewOps(\"\") unexpected error: %v", err)
	}
	if _, ok := ops.(*TuwunelMatrixOps); !ok {
		t.Fatalf("NewOps(\"\") = %T, want *TuwunelMatrixOps", ops)
	}
}

// TestNewOps_Tuwunel verifies explicit "tuwunel" selects TuwunelMatrixOps.
func TestNewOps_Tuwunel(t *testing.T) {
	ops, err := NewOps("tuwunel", Config{}, nil)
	if err != nil {
		t.Fatalf("NewOps(\"tuwunel\") unexpected error: %v", err)
	}
	if _, ok := ops.(*TuwunelMatrixOps); !ok {
		t.Fatalf("NewOps(\"tuwunel\") = %T, want *TuwunelMatrixOps", ops)
	}
}

// TestNewOps_Synapse verifies "synapse" selects SynapseMatrixOps.
func TestNewOps_Synapse(t *testing.T) {
	ops, err := NewOps("synapse", Config{}, nil)
	if err != nil {
		t.Fatalf("NewOps(\"synapse\") unexpected error: %v", err)
	}
	if _, ok := ops.(*SynapseMatrixOps); !ok {
		t.Fatalf("NewOps(\"synapse\") = %T, want *SynapseMatrixOps", ops)
	}
}

// TestNewOps_CaseInsensitive verifies the provider name is matched
// case-insensitively (mirrors config normalization).
func TestNewOps_CaseInsensitive(t *testing.T) {
	ops, err := NewOps("SYNAPSE", Config{}, nil)
	if err != nil {
		t.Fatalf("NewOps(\"SYNAPSE\") unexpected error: %v", err)
	}
	if _, ok := ops.(*SynapseMatrixOps); !ok {
		t.Fatalf("NewOps(\"SYNAPSE\") = %T, want *SynapseMatrixOps", ops)
	}
}

// TestNewOps_UnknownProviderErrors verifies an unrecognized provider value
// returns an error instead of silently picking an implementation.
func TestNewOps_UnknownProviderErrors(t *testing.T) {
	ops, err := NewOps("dendrite", Config{}, &http.Client{})
	if err == nil {
		t.Fatalf("NewOps(\"dendrite\") = %v, want error", ops)
	}
}
