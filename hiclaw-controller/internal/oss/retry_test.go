package oss

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/minio/minio-go/v7"
)

func TestRetryStorageOp_RetriesTransientThenSucceeds(t *testing.T) {
	ctx := context.Background()
	var calls atomic.Int32
	err := retryStorageOp(ctx, func() error {
		if calls.Add(1) <= 2 {
			return errors.New("dial tcp 10.0.0.1:9000: i/o timeout")
		}
		return nil
	})
	if err != nil {
		t.Fatalf("retryStorageOp: %v", err)
	}
	if calls.Load() != 3 {
		t.Errorf("calls = %d, want 3 (2 retries then success)", calls.Load())
	}
}

func TestRetryStorageOp_DeterministicErrorNotRetried(t *testing.T) {
	ctx := context.Background()
	var calls atomic.Int32
	err := retryStorageOp(ctx, func() error {
		calls.Add(1)
		return os.ErrNotExist
	})
	if !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("err = %v, want os.ErrNotExist", err)
	}
	if calls.Load() != 1 {
		t.Errorf("calls = %d, want 1 (deterministic errors never retried)", calls.Load())
	}
}

func TestRetryStorageOp_RespectsContextCancellation(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	var calls atomic.Int32
	var done atomic.Bool
	go func() {
		defer done.Store(true)
		err := retryStorageOp(ctx, func() error {
			calls.Add(1)
			return errors.New("connection refused")
		})
		if err == nil {
			t.Error("expected error after cancellation, got nil")
		}
	}()
	// Cancel after the first attempt lands, well inside the 30s window.
	for calls.Load() == 0 {
		time.Sleep(10 * time.Millisecond)
	}
	cancel()
	select {
	case <-time.After(5 * time.Second):
		t.Fatal("retryStorageOp did not abort on context cancellation")
	case <-waitDone(&done):
	}
}

func waitDone(d *atomic.Bool) <-chan struct{} {
	ch := make(chan struct{})
	go func() {
		for !d.Load() {
			time.Sleep(5 * time.Millisecond)
		}
		close(ch)
	}()
	return ch
}

func TestIsRetryableStorageError(t *testing.T) {
	cases := []struct {
		name string
		err  error
		want bool
	}{
		{"nil", nil, false},
		{"not exist", os.ErrNotExist, false},
		{"canceled", context.Canceled, false},
		{"dial timeout", errors.New("dial tcp 10.0.0.1:9000: i/o timeout"), true},
		{"connection refused", errors.New("connection refused"), true},
		{"reset", errors.New("connection reset by peer"), true},
		{"dns", errors.New("dial tcp: lookup s3.example.com: no such host"), true},
		{"eof", io.ErrUnexpectedEOF, true},
		{"s3 500 value", minio.ErrorResponse{StatusCode: http.StatusInternalServerError}, true},
		{"s3 503 value", minio.ErrorResponse{StatusCode: http.StatusServiceUnavailable}, true},
		{"s3 429 value", minio.ErrorResponse{StatusCode: http.StatusTooManyRequests}, true},
		{"s3 slow down ptr", &minio.ErrorResponse{Code: "SlowDown"}, true},
		{"s3 404 value", minio.ErrorResponse{StatusCode: http.StatusNotFound, Code: "NoSuchKey"}, false},
		{"s3 forbidden ptr", &minio.ErrorResponse{StatusCode: http.StatusForbidden, Code: "AccessDenied"}, false},
		{"wrapped s3 503 value", fmt.Errorf("outer: %w", minio.ErrorResponse{StatusCode: http.StatusServiceUnavailable}), true},
		{"wrapped s3 503 ptr", fmt.Errorf("outer: %w", &minio.ErrorResponse{StatusCode: http.StatusServiceUnavailable}), true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := isRetryableStorageError(tc.err); got != tc.want {
				t.Errorf("isRetryableStorageError(%v) = %v, want %v", tc.err, got, tc.want)
			}
		})
	}
}

func TestOpError_ConciseSingleLayer(t *testing.T) {
	cause := errors.New("dial tcp 10.0.0.1:9000: i/o timeout")
	oe := &OpError{Op: "get", Key: "agents/alice/SOUL.md", Cause: cause}
	msg := oe.Error()
	if strings.Count(msg, "storage get") != 1 {
		t.Errorf("expected single-layer storage message, got %q", msg)
	}
	if !strings.Contains(msg, "agents/alice/SOUL.md") || !strings.Contains(msg, "i/o timeout") {
		t.Errorf("message missing key or root cause: %q", msg)
	}
	// Unwrap chain preserved for errors.Is/As.
	if !errors.Is(oe, cause) {
		t.Error("errors.Is through OpError should reach the cause")
	}
}

func TestOpError_PreservesNotExistSemantics(t *testing.T) {
	oe := &OpError{Op: "get", Key: "agents/alice/openclaw.json", Cause: os.ErrNotExist}
	if !errors.Is(oe, os.ErrNotExist) {
		t.Error("errors.Is(err, os.ErrNotExist) must hold through OpError")
	}
}
