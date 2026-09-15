package oss

import (
	"context"
	"errors"
	"io"
	"net/http"
	"os"
	"time"

	"github.com/minio/minio-go/v7"
)

// The storage connect timeout, retry window, and backoff bounds are
// configurable via AGENTTEAMS_STORAGE_* environment variables (see storage_env.go);
// the defaults below document the tuned values and are what the env accessors
// fall back to. minio-go's default transport dials with Go's 30s timeout; on
// a flaky storage endpoint every retried request would stall 30s before
// failing, turning a reconcile into minutes of hard waits. 2s fails fast
// while remaining generous for a healthy endpoint.
//
// The retry window bounds the total wall-clock time spent retrying transient
// storage failures for one operation: a short OSS/cloud-S3 blip (dial
// timeout, connection reset, 5xx) is retried inside the window so the
// reconcile completes instead of failing and being requeued; a permanently
// dead endpoint still fails, but only after the full window. The initial
// pause between retries is 500ms, doubled per attempt up to a 5s cap.

// retryStorageOp runs fn, retrying transient network-class failures within
// the retry window budget. Deterministic errors (os.ErrNotExist,
// permission, malformed requests) are returned immediately — they would fail
// again on retry. The caller's ctx is honored between attempts; fn itself
// must respect ctx cancellation.
func retryStorageOp(ctx context.Context, fn func() error) error {
	_, err := retryStorageOpValue(ctx, func() (struct{}, error) {
		return struct{}{}, fn()
	})
	return err
}

// retryStorageOpValue is the value-returning variant of retryStorageOp used
// by operations that produce a result (e.g. the mc driver's stdout).
func retryStorageOpValue[T any](ctx context.Context, fn func() (T, error)) (T, error) {
	deadline := time.Now().Add(storageRetryWindow())
	backoff := storageRetryBackoffBase()
	for {
		v, err := fn()
		if err == nil || !isRetryableStorageError(err) {
			return v, err
		}
		remaining := time.Until(deadline)
		if remaining <= 0 {
			return v, err
		}
		delay := backoff
		if delay > remaining {
			delay = remaining
		}
		backoff *= 2
		if cap := storageRetryBackoffCap(); backoff > cap {
			backoff = cap
		}
		select {
		case <-ctx.Done():
			return v, ctx.Err()
		case <-time.After(delay):
		}
	}
}

// isRetryableStorageError reports whether a storage operation failure is
// transient — worth retrying inside retryStorageOp. Network-class failures
// (dial timeout, connection refused/reset, DNS) and server-side 5xx/rate-limit
// responses are retryable; deterministic 4xx object errors and
// os.ErrNotExist are not.
func isRetryableStorageError(err error) bool {
	if err == nil {
		return false
	}
	if errors.Is(err, os.ErrNotExist) || errors.Is(err, context.Canceled) {
		return false
	}
	if errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) {
		return true // truncated read mid-transfer
	}
	if isNetworkError(err) {
		return true
	}
	// minio-go may surface an S3 error as a value or a pointer; errors.As
	// only matches the concrete type in the chain, so check both forms.
	var resp minio.ErrorResponse
	if errors.As(err, &resp) && isRetryableS3Error(resp) {
		return true
	}
	var respPtr *minio.ErrorResponse
	if errors.As(err, &respPtr) && isRetryableS3Error(*respPtr) {
		return true
	}
	return false
}

// isRetryableS3Error reports whether an S3 ErrorResponse is transient
// (server-side 5xx or throttling) versus a deterministic object error.
func isRetryableS3Error(resp minio.ErrorResponse) bool {
	if resp.StatusCode >= http.StatusInternalServerError || resp.StatusCode == http.StatusTooManyRequests {
		return true
	}
	switch resp.Code {
	case "InternalError", "SlowDown", "ServiceUnavailable", "RequestTimeout", "RequestTimeTooSkewed", "OperationAborted":
		return true
	}
	return false
}
