package oss

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"strings"
)

// MinIOAdminClient implements StorageAdminClient for embedded-mode MinIO
// using the `mc admin` CLI. It is the legacy provider, selected together
// with the mc storage driver via HICLAW_STORAGE_DRIVER=mc; the default
// HICLAW_STORAGE_DRIVER=sdk uses SDKAdminClient (madmin-go) instead.
type MinIOAdminClient struct {
	config     Config
	aliasReady bool
}

// NewMinIOAdminClient creates a StorageAdminClient for managing MinIO users.
func NewMinIOAdminClient(cfg Config) *MinIOAdminClient {
	if cfg.MCBinary == "" {
		cfg.MCBinary = "mc"
	}
	if cfg.Alias == "" {
		cfg.Alias = "hiclaw"
	}
	return &MinIOAdminClient{config: cfg}
}

func (c *MinIOAdminClient) ensureAlias(ctx context.Context) error {
	if c.aliasReady || c.config.Endpoint == "" {
		return nil
	}
	cmd := exec.CommandContext(ctx, c.config.MCBinary, "alias", "set", c.config.Alias, c.config.Endpoint, c.config.AccessKey, c.config.SecretKey)
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("mc alias set: %w (stderr: %s)", err, strings.TrimSpace(stderr.String()))
	}
	c.aliasReady = true
	return nil
}

func (c *MinIOAdminClient) EnsureUser(ctx context.Context, username, password string) error {
	if err := c.ensureAlias(ctx); err != nil {
		return err
	}
	// mc admin user add is idempotent — updates password if user exists
	_, err := c.runMCAdmin(ctx, "user", "add", c.config.Alias, username, password)
	if err != nil && !strings.Contains(err.Error(), "already") {
		return fmt.Errorf("ensure minio user %s: %w", username, err)
	}
	return nil
}

func (c *MinIOAdminClient) EnsurePolicy(ctx context.Context, req PolicyRequest) error {
	if err := c.ensureAlias(ctx); err != nil {
		return err
	}
	policyName := "worker-" + req.WorkerName
	bucket := req.Bucket
	if bucket == "" {
		bucket = c.config.Bucket
	}

	policy := buildWorkerPolicy(req.WorkerName, bucket, req.TeamName, req.IsManager)
	policyJSON, err := json.MarshalIndent(policy, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal policy: %w", err)
	}

	policyFile, err := os.CreateTemp("", "hiclaw-policy-*.json")
	if err != nil {
		return fmt.Errorf("create policy temp file: %w", err)
	}
	defer os.Remove(policyFile.Name())

	if _, err := policyFile.Write(policyJSON); err != nil {
		policyFile.Close()
		return fmt.Errorf("write policy file: %w", err)
	}
	policyFile.Close()

	// Remove old policy (ignore errors), create new, then attach
	c.runMCAdmin(ctx, "policy", "remove", c.config.Alias, policyName)
	if _, err := c.runMCAdmin(ctx, "policy", "create", c.config.Alias, policyName, policyFile.Name()); err != nil {
		return fmt.Errorf("create policy %s: %w", policyName, err)
	}
	if _, err := c.runMCAdmin(ctx, "policy", "attach", c.config.Alias, policyName, "--user", req.WorkerName); err != nil {
		return fmt.Errorf("attach policy %s to user %s: %w", policyName, req.WorkerName, err)
	}
	return nil
}

func (c *MinIOAdminClient) DeleteUser(ctx context.Context, username string) error {
	if err := c.ensureAlias(ctx); err != nil {
		return err
	}
	policyName := "worker-" + username
	// Detach and remove policy first (ignore errors)
	c.runMCAdmin(ctx, "policy", "detach", c.config.Alias, policyName, "--user", username)
	c.runMCAdmin(ctx, "policy", "remove", c.config.Alias, policyName)
	// Remove user
	_, err := c.runMCAdmin(ctx, "user", "remove", c.config.Alias, username)
	if err != nil && !strings.Contains(err.Error(), "does not exist") {
		return fmt.Errorf("delete minio user %s: %w", username, err)
	}
	return nil
}

func (c *MinIOAdminClient) runMCAdmin(ctx context.Context, args ...string) (string, error) {
	fullArgs := append([]string{"admin"}, args...)
	cmd := exec.CommandContext(ctx, c.config.MCBinary, fullArgs...)
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	if err := cmd.Run(); err != nil {
		return "", fmt.Errorf("mc admin %s: %w (stderr: %s)",
			strings.Join(args, " "), err, strings.TrimSpace(stderr.String()))
	}
	return stdout.String(), nil
}
