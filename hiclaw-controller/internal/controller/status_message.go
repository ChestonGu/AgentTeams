package controller

import (
	"errors"
	"strings"

	"github.com/hiclaw/hiclaw-controller/internal/oss"
)

// statusMessageMaxLen caps Status.Message so a verbose root cause (e.g. a
// subprocess stderr dump) cannot bloat the CR object.
const statusMessageMaxLen = 512

// conciseStatusMessage reduces a reconcile error to a precise, single-layer
// message for CR Status.Message. Storage failures surface as the storage
// layer's own OpError ("storage get <key>: dial tcp ...: i/o timeout" — one
// layer, op + key + root cause); any other error falls back to its deepest
// non-opaque cause so the status does not carry the full multi-layer wrap
// chain that logs show. Opaque leaves (subprocess "exit status N") are
// skipped in favor of the frame that carries the actual diagnostic text.
func conciseStatusMessage(err error) string {
	if err == nil {
		return ""
	}
	var opErr *oss.OpError
	if errors.As(err, &opErr) {
		return truncateStatusMessage(opErr.Error())
	}

	// Walk to the deepest frame whose child is nil or opaque (subprocess
	// "exit status N"): the frame above an opaque leaf carries the actual
	// diagnostic text (e.g. mc's stderr), and a plain root cause is already
	// the most specific message.
	deepest := err
	for {
		u := errors.Unwrap(deepest)
		if u == nil || isOpaqueLeaf(u) {
			break
		}
		deepest = u
	}
	if !isOpaqueLeaf(deepest) {
		return truncateStatusMessage(deepest.Error())
	}
	return truncateStatusMessage(err.Error())
}

// isOpaqueLeaf reports errors whose bare text carries no diagnostic value
// without their wrapping context (subprocess exit codes, empty messages).
func isOpaqueLeaf(err error) bool {
	msg := err.Error()
	return msg == "" || strings.HasPrefix(msg, "exit status ")
}

func truncateStatusMessage(msg string) string {
	if len(msg) <= statusMessageMaxLen {
		return msg
	}
	return msg[:statusMessageMaxLen] + "...(truncated)"
}
