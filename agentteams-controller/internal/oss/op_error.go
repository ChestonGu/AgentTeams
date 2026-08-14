package oss

import (
	"errors"
	"fmt"
)

// OpError is the single-layer storage operation error surfaced to CR status.
// Storage failures used to bubble up as several nested wraps ("deploy package:
// package resolve/extract failed: resolve package failed to download ... :
// mc cp ...: exit status 1 (stderr: ...)"), which is precise in logs but
// noise in Status.Message. OpError keeps the chain intact for logs (Unwrap)
// while giving the reconcile boundary a concise, self-contained message:
//
//	storage get agentteams-config/packages/alice.zip: dial tcp 10.0.0.1:9000: i/o timeout
type OpError struct {
	Op    string // "get", "put", "putfile", "stat", "list", ...
	Key   string // object key (relative, as passed to the StorageClient)
	Cause error  // underlying error (dial timeout, S3 error response, ...)
}

// Error renders the concise single-layer message.
func (e *OpError) Error() string {
	if e.Key != "" {
		return fmt.Sprintf("storage %s %s: %v", e.Op, e.Key, e.Cause)
	}
	return fmt.Sprintf("storage %s: %v", e.Op, e.Cause)
}

// Unwrap preserves the cause chain for errors.Is/As and log stack walks.
func (e *OpError) Unwrap() error { return e.Cause }

// wrapOp converts a raw storage failure into a single-layer OpError carrying
// the operation and target key. Already-wrapped errors pass through.
func wrapOp(op, key string, err error) error {
	if err == nil {
		return nil
	}
	var oe *OpError
	if errors.As(err, &oe) {
		return err
	}
	return &OpError{Op: op, Key: key, Cause: err}
}
