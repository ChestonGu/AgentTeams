package oss

import (
	"context"
	"errors"
	"os"
	"testing"
)

// os.IsNotExist does NOT recurse through arbitrary Unwrap chains — it only
// peels *PathError/*LinkError/*SyscallError (see os.error.go underlyingErrorIs).
// Any wrapper around os.ErrNotExist therefore silently breaks service-layer
// first-create detection (deployer/legacy use os.IsNotExist to distinguish
// "object missing, generate-and-inject" from real storage failures).
func TestOsIsNotExistDoesNotUnwrapArbitrary(t *testing.T) {
	wrapped := &OpError{Op: "get", Key: "k", Cause: os.ErrNotExist}
	if os.IsNotExist(wrapped) {
		t.Fatal("os.IsNotExist unexpectedly unwrapped OpError")
	}
	if !errors.Is(wrapped, os.ErrNotExist) {
		t.Fatal("errors.Is must still unwrap OpError")
	}
}

// timedOp must pass os.ErrNotExist through unwrapped, since service-layer
// callers rely on os.IsNotExist to treat a missing object as first-create.
// Real failures still get the concise OpError wrapper for CR Status.Message.
func TestTimedOpPassesNotExistThrough(t *testing.T) {
	c := &SDKClient{}
	err := c.timedOp(context.Background(), "get", "agents/w/openclaw.json", func() error {
		return os.ErrNotExist
	})
	if err == nil {
		t.Fatal("expected an error")
	}
	if !os.IsNotExist(err) {
		t.Fatalf("os.IsNotExist(%v) = false, want true (first-create detection depends on this)", err)
	}
	var oe *OpError
	if errors.As(err, &oe) {
		t.Fatalf("os.ErrNotExist must not be wrapped in OpError, got %v", err)
	}

	real := c.timedOp(context.Background(), "get", "k", func() error {
		return os.ErrPermission
	})
	var realOE *OpError
	if !errors.As(real, &realOE) {
		t.Fatalf("real storage failures should be wrapped in OpError, got %v", real)
	}
}
