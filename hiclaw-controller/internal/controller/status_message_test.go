package controller

import (
	"errors"
	"fmt"
	"os"
	"strings"
	"testing"

	"github.com/hiclaw/hiclaw-controller/internal/oss"
)

func TestConciseStatusMessage_StorageOpError(t *testing.T) {
	// Simulates the pre-fix multi-layer chain: reconcile → deploy package →
	// resolve/extract → download → storage OpError (single layer at the root).
	leaf := errors.New("dial tcp 10.0.0.1:9000: i/o timeout")
	opErr := &oss.OpError{Op: "get", Key: "hiclaw-config/packages/alice.zip", Cause: leaf}
	chain := fmt.Errorf("deploy package: %w", fmt.Errorf("package resolve/extract failed: %w", opErr))

	got := conciseStatusMessage(chain)
	want := "storage get hiclaw-config/packages/alice.zip: dial tcp 10.0.0.1:9000: i/o timeout"
	if got != want {
		t.Errorf("conciseStatusMessage = %q, want %q", got, want)
	}
}

func TestConciseStatusMessage_RootCauseFallback(t *testing.T) {
	chain := fmt.Errorf("provision worker: %w", errors.New("M_FORBIDDEN: user already in room"))
	got := conciseStatusMessage(chain)
	if got != "M_FORBIDDEN: user already in room" {
		t.Errorf("conciseStatusMessage = %q, want root cause", got)
	}
}

func TestConciseStatusMessage_SkipsOpaqueLeaf(t *testing.T) {
	// Models the real failure from the test logs: deploy package →
	// resolve/extract → mc frame with stderr → opaque "exit status 1" leaf.
	// The status must surface only the single frame carrying the diagnostic
	// (the mc stderr), not the full multi-layer stack.
	leaf := fmt.Errorf("exit status 1")
	mcErr := fmt.Errorf("mc cp hiclaw/hiclaw-storage/hiclaw-config/packages/alice.zip: %w (stderr: mc: unable to prepare URL for copying)", leaf)
	chain := fmt.Errorf("deploy package: %w", fmt.Errorf("package resolve/extract failed: %w", mcErr))

	got := conciseStatusMessage(chain)
	if !strings.Contains(got, "unable to prepare URL") {
		t.Errorf("conciseStatusMessage = %q, want the frame carrying the stderr diagnostic", got)
	}
	if strings.Contains(got, "deploy package") || strings.Contains(got, "package resolve/extract") {
		t.Errorf("conciseStatusMessage = %q, must not carry the outer wrap chain", got)
	}
}

func TestConciseStatusMessage_TruncatesLongMessages(t *testing.T) {
	long := strings.Repeat("x", statusMessageMaxLen*2)
	got := conciseStatusMessage(fmt.Errorf("boom: %w", errors.New(long)))
	if len(got) > statusMessageMaxLen+len("...(truncated)") {
		t.Errorf("message length %d exceeds cap", len(got))
	}
	if !strings.HasSuffix(got, "...(truncated)") {
		t.Errorf("expected truncation suffix, got %q", got)
	}
}

func TestConciseStatusMessage_Nil(t *testing.T) {
	if got := conciseStatusMessage(nil); got != "" {
		t.Errorf("conciseStatusMessage(nil) = %q, want empty", got)
	}
}

func TestConciseStatusMessage_NotExistStaysNotExistLike(t *testing.T) {
	// os.ErrNotExist wrapped in an OpError must still be recognized by
	// callers that probe with errors.Is; the status text stays the op error.
	opErr := &oss.OpError{Op: "get", Key: "agents/alice/SOUL.md", Cause: os.ErrNotExist}
	if !errors.Is(opErr, os.ErrNotExist) {
		t.Error("errors.Is must penetrate OpError")
	}
}
