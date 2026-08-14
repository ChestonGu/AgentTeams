package oss

import (
	"os"
	"strconv"
	"time"
)

// Storage tuning knobs are environment variables so operators can adjust them
// per deployment without a rebuild. Defaults preserve the tuned values from
// the storage resilience work (2s connect, 30s retry window, 0.5s→5s backoff).
// All values are read at call time: the SDK transport and retry loops are
// constructed once at startup, so the per-call os.Getenv cost is negligible,
// and tests can override them via os.Setenv.

const (
	// envStorageConnectTimeoutSeconds bounds a single TCP/TLS connection
	// attempt (default 2s).
	envStorageConnectTimeoutSeconds = "AGENTTEAMS_STORAGE_CONNECT_TIMEOUT_SECONDS"
	// envStorageRetryWindowSeconds bounds the total wall-clock time spent
	// retrying transient storage failures for one operation (default 30s).
	envStorageRetryWindowSeconds = "AGENTTEAMS_STORAGE_RETRY_WINDOW_SECONDS"
	// envStorageRetryBackoffMS is the initial pause between retries, doubled
	// per attempt up to the cap (default 500).
	envStorageRetryBackoffMS = "AGENTTEAMS_STORAGE_RETRY_BACKOFF_MS"
	// envStorageRetryBackoffMaxMS bounds the per-attempt pause (default 5000).
	envStorageRetryBackoffMaxMS = "AGENTTEAMS_STORAGE_RETRY_BACKOFF_MAX_MS"
	// envStorageSDKMaxRetries bounds minio-go's internal automatic retries
	// (default 2); the outer retry window is the primary resilience layer.
	envStorageSDKMaxRetries = "AGENTTEAMS_STORAGE_SDK_MAX_RETRIES"
	// envStorageProbeTimeoutSeconds bounds the reachability probe before the
	// config phase (default 30s, matching the retry window).
	envStorageProbeTimeoutSeconds = "AGENTTEAMS_STORAGE_PROBE_TIMEOUT_SECONDS"
)

// storageConnectTimeout returns the per-connection dial/TLS budget.
func storageConnectTimeout() time.Duration {
	return secondsEnv(envStorageConnectTimeoutSeconds, 2)
}

// storageRetryWindow returns the total retry budget for one storage op.
func storageRetryWindow() time.Duration {
	return secondsEnv(envStorageRetryWindowSeconds, 30)
}

// storageRetryBackoffBase returns the initial retry pause.
func storageRetryBackoffBase() time.Duration {
	return millisEnv(envStorageRetryBackoffMS, 500)
}

// storageRetryBackoffCap returns the per-attempt retry pause ceiling.
func storageRetryBackoffCap() time.Duration {
	return millisEnv(envStorageRetryBackoffMaxMS, 5000)
}

// sdkMaxRetries returns minio-go's internal retry budget.
func sdkMaxRetries() int {
	return positiveIntEnv(envStorageSDKMaxRetries, 2)
}

// StorageProbeTimeout returns the storage reachability probe budget used by
// the deployer's config phase. Exported for the service package.
func StorageProbeTimeout() time.Duration {
	return secondsEnv(envStorageProbeTimeoutSeconds, 30)
}

// secondsEnv parses a positive integer seconds env var; malformed or
// non-positive values fall back to def.
func secondsEnv(key string, def int) time.Duration {
	return time.Duration(positiveIntEnv(key, def)) * time.Second
}

// millisEnv parses a positive integer milliseconds env var; malformed or
// non-positive values fall back to def.
func millisEnv(key string, def int) time.Duration {
	return time.Duration(positiveIntEnv(key, def)) * time.Millisecond
}

// positiveIntEnv parses an env var as a positive integer, falling back to def
// when unset, non-numeric, or <= 0 (a zero/negative knob has no useful
// meaning for timeouts, windows, or retry counts).
func positiveIntEnv(key string, def int) int {
	v, ok := os.LookupEnv(key)
	if !ok || v == "" {
		return def
	}
	n, err := strconv.Atoi(v)
	if err != nil || n <= 0 {
		return def
	}
	return n
}
