package oss

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"github.com/minio/madmin-go/v3"
	"github.com/minio/minio-go/v7/pkg/credentials"
)

// SDKAdminClient implements StorageAdminClient on top of the MinIO Admin API
// (madmin-go SDK), replacing the `mc admin` subprocess forks with the same
// connection-pooled transport and fast-fail dial timeout used by the SDK
// storage driver. It is the default admin provider, selected by
// HICLAW_STORAGE_DRIVER=sdk; HICLAW_STORAGE_DRIVER=mc selects
// MinIOAdminClient instead.
type SDKAdminClient struct {
	config Config
	client *madmin.AdminClient
}

// NewSDKAdminClient creates a StorageAdminClient for managing MinIO users.
// Like NewSDKClient, the admin client is built eagerly; construction only
// parses the endpoint and never performs network I/O.
func NewSDKAdminClient(cfg Config) (*SDKAdminClient, error) {
	endpoint, secure := endpointHost(cfg.Endpoint)
	cli, err := madmin.NewWithOptions(endpoint, &madmin.Options{
		Creds:     credentials.NewStaticV4(cfg.AccessKey, cfg.SecretKey, ""),
		Secure:    secure,
		Transport: sdkTransport(),
	})
	if err != nil {
		return nil, fmt.Errorf("create minio admin client for %s: %w", endpoint, err)
	}
	return &SDKAdminClient{config: cfg, client: cli}, nil
}

// EnsureUser creates the user, or updates the password when the user already
// exists — matching `mc admin user add`'s idempotent semantics.
func (c *SDKAdminClient) EnsureUser(ctx context.Context, username, password string) error {
	err := c.client.AddUser(ctx, username, password)
	if err != nil {
		if !isAdminAlreadyExists(err) {
			return fmt.Errorf("ensure minio user %s: %w", username, err)
		}
		if err := c.client.SetUser(ctx, username, password, madmin.AccountEnabled); err != nil {
			return fmt.Errorf("update minio user %s: %w", username, err)
		}
	}
	return nil
}

// EnsurePolicy replaces the worker's scoped policy and attaches it to the
// user. Mirrors the `mc admin policy remove/create/attach` sequence used by
// MinIOAdminClient.
func (c *SDKAdminClient) EnsurePolicy(ctx context.Context, req PolicyRequest) error {
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

	// Drop any previous version before recreating. Removal errors are ignored
	// (the policy may not exist yet); if the server refuses to remove a
	// still-attached policy, detach it from this worker first.
	if err := c.client.RemoveCannedPolicy(ctx, policyName); err != nil && !isAdminNotExists(err) {
		_, _ = c.client.DetachPolicy(ctx, madmin.PolicyAssociationReq{
			Policies: []string{policyName},
			User:     req.WorkerName,
		})
		_ = c.client.RemoveCannedPolicy(ctx, policyName)
	}
	if err := c.client.AddCannedPolicy(ctx, policyName, policyJSON); err != nil {
		return fmt.Errorf("create policy %s: %w", policyName, err)
	}
	if _, err := c.client.AttachPolicy(ctx, madmin.PolicyAssociationReq{
		Policies: []string{policyName},
		User:     req.WorkerName,
	}); err != nil {
		return fmt.Errorf("attach policy %s to user %s: %w", policyName, req.WorkerName, err)
	}
	return nil
}

// DeleteUser detaches and removes the worker's policy, then removes the user.
// All cleanup steps tolerate missing entities.
func (c *SDKAdminClient) DeleteUser(ctx context.Context, username string) error {
	policyName := "worker-" + username
	// Detach and remove policy first (ignore errors).
	_, _ = c.client.DetachPolicy(ctx, madmin.PolicyAssociationReq{
		Policies: []string{policyName},
		User:     username,
	})
	_ = c.client.RemoveCannedPolicy(ctx, policyName)
	if err := c.client.RemoveUser(ctx, username); err != nil && !isAdminNotExists(err) {
		return fmt.Errorf("delete minio user %s: %w", username, err)
	}
	return nil
}

// isAdminAlreadyExists reports whether err is a MinIO admin "entity already
// exists" response, matched by server error code with a message-substring
// fallback for servers that do not return a structured code.
func isAdminAlreadyExists(err error) bool {
	if err == nil {
		return false
	}
	var resp madmin.ErrorResponse
	if errors.As(err, &resp) {
		if resp.Code == "UserAlreadyExists" || resp.Code == "PolicyAlreadyExists" {
			return true
		}
	}
	return strings.Contains(err.Error(), "already")
}

// isAdminNotExists reports whether err is a MinIO admin "entity does not
// exist" response (same code/substring strategy as isAdminAlreadyExists).
func isAdminNotExists(err error) bool {
	if err == nil {
		return false
	}
	var resp madmin.ErrorResponse
	if errors.As(err, &resp) {
		if resp.Code == "NoSuchUser" || resp.Code == "NoSuchPolicy" {
			return true
		}
	}
	return strings.Contains(err.Error(), "does not exist")
}
