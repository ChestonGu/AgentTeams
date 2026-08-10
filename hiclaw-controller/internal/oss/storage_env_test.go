package oss

import (
	"testing"
	"time"
)

func TestStorageEnv_Defaults(t *testing.T) {
	if got := storageConnectTimeout(); got != 2*time.Second {
		t.Errorf("storageConnectTimeout = %v, want 2s", got)
	}
	if got := storageRetryWindow(); got != 30*time.Second {
		t.Errorf("storageRetryWindow = %v, want 30s", got)
	}
	if got := storageRetryBackoffBase(); got != 500*time.Millisecond {
		t.Errorf("storageRetryBackoffBase = %v, want 500ms", got)
	}
	if got := storageRetryBackoffCap(); got != 5*time.Second {
		t.Errorf("storageRetryBackoffCap = %v, want 5s", got)
	}
	if got := sdkMaxRetries(); got != 2 {
		t.Errorf("sdkMaxRetries = %d, want 2", got)
	}
	if got := StorageProbeTimeout(); got != 30*time.Second {
		t.Errorf("StorageProbeTimeout = %v, want 30s", got)
	}
}

func TestStorageEnv_Override(t *testing.T) {
	t.Setenv(envStorageConnectTimeoutSeconds, "5")
	t.Setenv(envStorageRetryWindowSeconds, "60")
	t.Setenv(envStorageRetryBackoffMS, "250")
	t.Setenv(envStorageRetryBackoffMaxMS, "10000")
	t.Setenv(envStorageSDKMaxRetries, "4")
	t.Setenv(envStorageProbeTimeoutSeconds, "45")

	if got := storageConnectTimeout(); got != 5*time.Second {
		t.Errorf("storageConnectTimeout = %v, want 5s", got)
	}
	if got := storageRetryWindow(); got != 60*time.Second {
		t.Errorf("storageRetryWindow = %v, want 60s", got)
	}
	if got := storageRetryBackoffBase(); got != 250*time.Millisecond {
		t.Errorf("storageRetryBackoffBase = %v, want 250ms", got)
	}
	if got := storageRetryBackoffCap(); got != 10*time.Second {
		t.Errorf("storageRetryBackoffCap = %v, want 10s", got)
	}
	if got := sdkMaxRetries(); got != 4 {
		t.Errorf("sdkMaxRetries = %d, want 4", got)
	}
	if got := StorageProbeTimeout(); got != 45*time.Second {
		t.Errorf("StorageProbeTimeout = %v, want 45s", got)
	}
}

func TestStorageEnv_InvalidFallsBackToDefault(t *testing.T) {
	for _, key := range []string{
		envStorageConnectTimeoutSeconds,
		envStorageRetryWindowSeconds,
		envStorageRetryBackoffMS,
		envStorageRetryBackoffMaxMS,
		envStorageSDKMaxRetries,
		envStorageProbeTimeoutSeconds,
	} {
		for _, bad := range []string{"", "abc", "-1", "0", "1.5"} {
			t.Setenv(key, bad)
			// All knobs share positiveIntEnv, so any env set to a bad value
			// must fall back; verify one representative accessor per key.
			if got := positiveIntEnv(key, 42); got != 42 {
				t.Errorf("%s=%q → %d, want default 42", key, bad, got)
			}
		}
	}
}
