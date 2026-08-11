package oss

import (
	"errors"
	"os"
	"testing"
)

func TestMinIOClient_FullPath(t *testing.T) {
	c := NewMinIOClient(Config{
		StoragePrefix: "hiclaw/hiclaw-storage",
	})

	got := c.fullPath("agents/worker-1/openclaw.json")
	want := "hiclaw/hiclaw-storage/agents/worker-1/openclaw.json"
	if got != want {
		t.Errorf("fullPath = %q, want %q", got, want)
	}
}

func TestMinIOClient_FullPathNoLeadingSlash(t *testing.T) {
	c := NewMinIOClient(Config{
		StoragePrefix: "hiclaw/hiclaw-storage",
	})

	got := c.fullPath("/agents/worker-1/file.txt")
	want := "hiclaw/hiclaw-storage/agents/worker-1/file.txt"
	if got != want {
		t.Errorf("fullPath with leading slash = %q, want %q", got, want)
	}
}

func TestBuildWorkerPolicy(t *testing.T) {
	policy := buildWorkerPolicy("worker-1", "hiclaw-storage", "team-dev", false)

	if policy.Version != "2012-10-17" {
		t.Errorf("Version = %q", policy.Version)
	}
	if len(policy.Statement) != 2 {
		t.Fatalf("expected 2 statements, got %d", len(policy.Statement))
	}

	// Verify team prefix is included in list conditions
	listStmt := policy.Statement[0]
	condition := listStmt.Condition["StringLike"].(map[string]interface{})
	prefixes := condition["s3:prefix"].([]string)
	hasTeam := false
	for _, p := range prefixes {
		if p == "teams/team-dev" || p == "teams/team-dev/*" {
			hasTeam = true
			break
		}
	}
	if !hasTeam {
		t.Errorf("expected team prefix in list conditions: %v", prefixes)
	}

	// Verify team resource in RW statement
	rwStmt := policy.Statement[1]
	hasTeamResource := false
	for _, r := range rwStmt.Resource {
		if r == "arn:aws:s3:::hiclaw-storage/teams/team-dev/*" {
			hasTeamResource = true
			break
		}
	}
	if !hasTeamResource {
		t.Errorf("expected team resource in RW statement: %v", rwStmt.Resource)
	}
}

func TestBuildWorkerPolicyNoTeam(t *testing.T) {
	policy := buildWorkerPolicy("worker-solo", "hiclaw-storage", "", false)

	rwStmt := policy.Statement[1]
	for _, r := range rwStmt.Resource {
		if r == "arn:aws:s3:::hiclaw-storage/teams/*" {
			t.Error("solo worker should not have team resource")
		}
		if r == "arn:aws:s3:::hiclaw-storage/manager/*" {
			t.Error("non-manager worker should not have manager resource")
		}
	}
}

func TestBuildManagerPolicy(t *testing.T) {
	policy := buildWorkerPolicy("default", "hiclaw-storage", "", true)

	if len(policy.Statement) != 2 {
		t.Fatalf("expected 2 statements, got %d", len(policy.Statement))
	}

	// Verify manager prefix in list conditions
	listStmt := policy.Statement[0]
	condition := listStmt.Condition["StringLike"].(map[string]interface{})
	prefixes := condition["s3:prefix"].([]string)
	hasManager := false
	for _, p := range prefixes {
		if p == "manager" || p == "manager/*" {
			hasManager = true
			break
		}
	}
	if !hasManager {
		t.Errorf("expected manager prefix in list conditions: %v", prefixes)
	}

	// Verify manager resource in RW statement
	rwStmt := policy.Statement[1]
	hasManagerResource := false
	for _, r := range rwStmt.Resource {
		if r == "arn:aws:s3:::hiclaw-storage/manager/*" {
			hasManagerResource = true
			break
		}
	}
	if !hasManagerResource {
		t.Errorf("expected manager resource in RW statement: %v", rwStmt.Resource)
	}
}

func TestNewMinIOClient_Defaults(t *testing.T) {
	c := NewMinIOClient(Config{})
	if c.config.MCBinary != "mc" {
		t.Errorf("MCBinary = %q, want mc", c.config.MCBinary)
	}
	if c.config.Alias != "hiclaw" {
		t.Errorf("Alias = %q, want hiclaw", c.config.Alias)
	}
}

func TestClassifyStorageError(t *testing.T) {
	cases := []struct {
		name string
		err  error
		want string
	}{
		{"nil", nil, ""},
		{"not exist", os.ErrNotExist, "not_found"},
		{"dial timeout", errors.New("dial tcp 10.0.0.1:9000: i/o timeout"), "network"},
		{"connection refused", errors.New("mc cp: connection refused"), "network"},
		{"no such host", errors.New("dial tcp: lookup s3.example.com: no such host"), "network"},
		{"context deadline", errors.New("context deadline exceeded"), "network"},
		{"op timeout", errors.New("request timed out after 30s"), "timeout"},
		{"s3 500", errors.New("mc cat: The request failed (500)"), "other"},
		{"permission", errors.New("mc cp: AccessDenied"), "other"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := classifyStorageError(tc.err); got != tc.want {
				t.Errorf("classifyStorageError(%v) = %q, want %q", tc.err, got, tc.want)
			}
		})
	}
}
