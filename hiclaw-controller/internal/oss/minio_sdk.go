package oss

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/hiclaw/hiclaw-controller/internal/metrics"
	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
)

// SDKClient implements StorageClient on top of the minio-go SDK, replacing the
// per-operation `mc` subprocess fork (exec + TLS handshake + no connection
// pool) with a connection-pooled HTTP client. Benchmarks on Aliyun OSS
// measured ~5.8x lower per-member config-round latency than the mc driver.
// Select via HICLAW_STORAGE_DRIVER=sdk (default); HICLAW_STORAGE_DRIVER=mc
// restores the MinIOClient implementation. The StorageClient contract and
// Config semantics are identical for both drivers.
//
// Credential modes mirror MinIOClient:
//   - Static (default): AccessKey/SecretKey from Config, fixed at construction.
//   - Dynamic (credSource != nil): credentials resolved via a
//     credentials.Provider whose IsExpired always returns true, so minio-go
//     refreshes before every request; the underlying TokenManager caches STS
//     tokens until near expiry, so the refresh is cheap in steady state.
type SDKClient struct {
	cfg        Config
	credSource CredentialSource
	client     *minio.Client
}

// NewSDKClient creates a StorageClient backed by the minio-go SDK.
func NewSDKClient(cfg Config) (*SDKClient, error) {
	if cfg.Alias == "" {
		cfg.Alias = "hiclaw"
	}
	client, err := newMinioClient(cfg, nil)
	if err != nil {
		return nil, err
	}
	return &SDKClient{cfg: cfg, client: client}, nil
}

// WithCredentialSource returns a copy of the client that resolves credentials
// dynamically on every operation (external-OSS STS deployments). Matches
// MinIOClient.WithCredentialSource.
func (c *SDKClient) WithCredentialSource(src CredentialSource) *SDKClient {
	client, err := newMinioClient(c.cfg, src)
	if err != nil {
		// Config was already validated at construction; a rebuild can only
		// fail on endpoint parsing, which cannot change. Keep the previous
		// client rather than returning a broken one.
		return &SDKClient{cfg: c.cfg, credSource: src, client: c.client}
	}
	return &SDKClient{cfg: c.cfg, credSource: src, client: client}
}

// The connect timeout bounds a single TCP/TLS connection attempt. minio-go's
// default transport dials with Go's 30s timeout; on a flaky storage endpoint
// every retried request would stall 30s before failing, turning a reconcile
// into minutes of hard waits. The default 2s fails fast while remaining
// generous for a healthy endpoint; configurable via
// HICLAW_STORAGE_CONNECT_TIMEOUT_SECONDS (see storage_env.go). Connection
// pooling only reuses live connections — a pool miss (concurrency pressure
// or stale idle conns dropped by the network) dials anew, which is exactly
// when this bound matters.

// sdkMaxRetries bounds minio-go's internal automatic retries. The outer
// retryStorageOp wrapper (retry window) is the primary resilience layer; this
// small internal budget covers immediate retries without waiting for the
// wrapper's backoff. Configurable via HICLAW_STORAGE_SDK_MAX_RETRIES.

// sdkTransport builds the shared HTTP transport for SDKClient: connection
// pool for reuse (the whole point of the SDK over per-call mc forks) plus a
// fast-fail dial so an unreachable endpoint does not stall the reconcile.
func sdkTransport() *http.Transport {
	dialTimeout := storageConnectTimeout()
	return &http.Transport{
		Proxy: http.ProxyFromEnvironment,
		DialContext: (&net.Dialer{
			Timeout:   dialTimeout,
			KeepAlive: 30 * time.Second,
		}).DialContext,
		ForceAttemptHTTP2:     true,
		MaxIdleConns:          100,
		MaxIdleConnsPerHost:   10,
		IdleConnTimeout:       30 * time.Second,
		TLSHandshakeTimeout:   dialTimeout,
		ExpectContinueTimeout: 1 * time.Second,
		// Bounds waiting for a server response header; measured after the
		// request body is fully written, so long uploads are not affected.
		ResponseHeaderTimeout: 30 * time.Second,
	}
}

// endpointHost extracts the bare host and TLS flag from a MinIO/S3 endpoint
// URL. Both the minio-go and madmin clients take the host with a separate
// secure flag, so a scheme prefix ("http://minio:9000") must be stripped
// before construction.
func endpointHost(endpoint string) (host string, secure bool) {
	if u, err := url.Parse(endpoint); err == nil && u.Scheme != "" {
		if u.Host != "" {
			return u.Host, u.Scheme == "https"
		}
	}
	return endpoint, false
}

func newMinioClient(cfg Config, src CredentialSource) (*minio.Client, error) {
	endpoint, secure := endpointHost(cfg.Endpoint)
	var creds *credentials.Credentials
	if src != nil {
		creds = credentials.New(&credSourceProvider{src: src})
	} else {
		creds = credentials.NewStaticV4(cfg.AccessKey, cfg.SecretKey, "")
	}
	client, err := minio.New(endpoint, &minio.Options{
		Creds:      creds,
		Secure:     secure,
		Transport:  sdkTransport(),
		MaxRetries: sdkMaxRetries(),
	})
	if err != nil {
		return nil, fmt.Errorf("create minio-go client for %s: %w", endpoint, err)
	}
	return client, nil
}

// credSourceProvider adapts an oss.CredentialSource to minio-go's
// credentials.Provider. IsExpired always returns true so minio-go fetches
// fresh credentials before every request; the underlying TokenManager caches
// STS tokens until near expiry, so the per-request refresh is a cheap mutex +
// time comparison. context.Background is used because the Provider interface
// carries no context; the TokenManager only performs network I/O on a cache
// miss (roughly once per token lifetime).
type credSourceProvider struct {
	src CredentialSource
}

func (p *credSourceProvider) Retrieve() (credentials.Value, error) {
	return p.retrieve()
}

func (p *credSourceProvider) RetrieveWithCredContext(*credentials.CredContext) (credentials.Value, error) {
	return p.retrieve()
}

func (p *credSourceProvider) retrieve() (credentials.Value, error) {
	c, err := p.src.Resolve(context.Background())
	if err != nil {
		return credentials.Value{}, err
	}
	return credentials.Value{
		AccessKeyID:     c.AccessKeyID,
		SecretAccessKey: c.AccessKeySecret,
		SessionToken:    c.SecurityToken,
		SignerType:      credentials.SignatureV4,
	}, nil
}

func (p *credSourceProvider) IsExpired() bool { return true }

// bucketAndKey maps a StorageClient key to the (bucket, object) pair the
// minio-go API expects. Config.StoragePrefix follows the mc "alias/bucket"
// form (e.g. "hiclaw/hiclaw-storage"); the alias segment is stripped and the
// bucket segment (or Config.Bucket, when set) becomes the bucket, with any
// remaining prefix segments joined to the object key.
func (c *SDKClient) bucketAndKey(key string) (string, string) {
	bucket := c.cfg.Bucket
	if bucket == "" {
		seg := strings.SplitN(strings.Trim(c.cfg.StoragePrefix, "/"), "/", 3)
		if len(seg) >= 2 {
			bucket = seg[1]
		}
	}
	rest := strings.TrimPrefix(strings.Trim(c.cfg.StoragePrefix, "/"), c.cfg.Alias+"/")
	rest = strings.TrimPrefix(rest, bucket)
	objPrefix := strings.Trim(rest, "/")
	obj := strings.Trim(strings.TrimPrefix(key, "/"), "/")
	if objPrefix != "" {
		obj = objPrefix + "/" + obj
	}
	return bucket, obj
}

// mapNotExist converts minio-go NoSuchKey/NotFound responses to os.ErrNotExist,
// matching the mc driver's GetObject/Stat contract (callers rely on
// errors.Is(err, os.ErrNotExist)).
func mapNotExist(err error) error {
	if err == nil {
		return nil
	}
	if errors.Is(err, os.ErrNotExist) {
		return err
	}
	var resp minio.ErrorResponse
	if errors.As(err, &resp) {
		if resp.Code == "NoSuchKey" || resp.Code == "NotFound" || resp.StatusCode == http.StatusNotFound {
			return os.ErrNotExist
		}
	}
	return err
}

// timedOp observes one storage operation for the stability metrics
// (hiclaw_storage_op_duration_seconds / hiclaw_storage_op_errors_total),
// retries transient failures within the storageRetryWindow, and wraps the
// final error in a single-layer OpError for concise CR Status.Message text.
//
// os.ErrNotExist is passed through unwrapped: service-layer callers use
// os.IsNotExist to distinguish "object missing (first create)" from real
// storage failures, and os.IsNotExist only peels *PathError/*LinkError/
// *SyscallError — it does NOT recurse Unwrap — so wrapping ErrNotExist in
// an OpError makes every first-create check fail and aborts deploys that
// should generate-and-inject instead.
func (c *SDKClient) timedOp(ctx context.Context, op, key string, fn func() error) error {
	start := time.Now()
	err := retryStorageOp(ctx, fn)
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		err = &OpError{Op: op, Key: key, Cause: err}
	}
	metrics.StorageOpDuration.WithLabelValues(op, "sdk").Observe(time.Since(start).Seconds())
	if err != nil {
		metrics.StorageOpErrors.WithLabelValues(op, "sdk", classifyStorageError(err)).Inc()
	}
	return err
}

func (c *SDKClient) PutObject(ctx context.Context, key string, data []byte) error {
	return c.timedOp(ctx, "put", key, func() error {
		bucket, obj := c.bucketAndKey(key)
		_, err := c.client.PutObject(ctx, bucket, obj, bytes.NewReader(data), int64(len(data)), minio.PutObjectOptions{})
		return err
	})
}

func (c *SDKClient) PutFile(ctx context.Context, localPath, key string) error {
	return c.timedOp(ctx, "putfile", key, func() error {
		bucket, obj := c.bucketAndKey(key)
		_, err := c.client.FPutObject(ctx, bucket, obj, localPath, minio.PutObjectOptions{})
		return err
	})
}

func (c *SDKClient) GetObject(ctx context.Context, key string) ([]byte, error) {
	var data []byte
	err := c.timedOp(ctx, "get", key, func() error {
		bucket, obj := c.bucketAndKey(key)
		rc, err := c.client.GetObject(ctx, bucket, obj, minio.GetObjectOptions{})
		if err != nil {
			return mapNotExist(err)
		}
		defer rc.Close()
		data, err = io.ReadAll(rc)
		return mapNotExist(err)
	})
	return data, err
}

func (c *SDKClient) GetETag(ctx context.Context, key string) (string, error) {
	var etag string
	err := c.timedOp(ctx, "stat", key, func() error {
		bucket, obj := c.bucketAndKey(key)
		info, err := c.client.StatObject(ctx, bucket, obj, minio.StatObjectOptions{})
		if err != nil {
			return mapNotExist(err)
		}
		// Multipart uploads yield "<md5>-N"; strip the part suffix to match
		// the mc stat --json extraction the package resolver relied on.
		etag = strings.ReplaceAll(info.ETag, "-", "")
		return nil
	})
	return etag, err
}

func (c *SDKClient) Stat(ctx context.Context, key string) error {
	return c.timedOp(ctx, "stat", key, func() error {
		bucket, obj := c.bucketAndKey(key)
		_, err := c.client.StatObject(ctx, bucket, obj, minio.StatObjectOptions{})
		return mapNotExist(err)
	})
}

func (c *SDKClient) DeleteObject(ctx context.Context, key string) error {
	return c.timedOp(ctx, "delete", key, func() error {
		bucket, obj := c.bucketAndKey(key)
		return c.client.RemoveObject(ctx, bucket, obj, minio.RemoveObjectOptions{})
	})
}

func (c *SDKClient) DeletePrefix(ctx context.Context, prefix string) error {
	return c.timedOp(ctx, "deleteprefix", prefix, func() error {
		bucket, objPrefix := c.bucketAndKey(prefix)
		for obj := range c.client.ListObjects(ctx, bucket, minio.ListObjectsOptions{Prefix: objPrefix, Recursive: true}) {
			if obj.Err != nil {
				return obj.Err
			}
			if err := c.client.RemoveObject(ctx, bucket, obj.Key, minio.RemoveObjectOptions{}); err != nil {
				return err
			}
		}
		return nil
	})
}

func (c *SDKClient) ListObjects(ctx context.Context, prefix string) ([]string, error) {
	var names []string
	err := c.timedOp(ctx, "list", prefix, func() error {
		bucket, objPrefix := c.bucketAndKey(prefix)
		for obj := range c.client.ListObjects(ctx, bucket, minio.ListObjectsOptions{Prefix: objPrefix, Recursive: false}) {
			if obj.Err != nil {
				return obj.Err
			}
			// mc ls (non-recursive) returns the leaf name; trim the prefix to match.
			names = append(names, strings.TrimPrefix(obj.Key, objPrefix))
		}
		return nil
	})
	return names, err
}

// Mirror mirrors a local directory tree (src starting with "/", as all
// production callers pass) or a remote prefix (src without a leading "/")
// into the dst prefix. Overwrite semantics match the mc driver.
func (c *SDKClient) Mirror(ctx context.Context, src, dst string, opts MirrorOptions) error {
	return c.timedOp(ctx, "mirror", dst, func() error {
		if strings.HasPrefix(src, "/") {
			return c.mirrorLocalToRemote(ctx, src, dst, opts)
		}
		return c.mirrorRemoteToRemote(ctx, src, dst, opts)
	})
}

func (c *SDKClient) mirrorLocalToRemote(ctx context.Context, srcDir, dst string, opts MirrorOptions) error {
	bucket, dstPrefix := c.bucketAndKey(dst)
	return filepath.WalkDir(srcDir, func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if entry.IsDir() || entry.Type()&os.ModeSymlink != 0 {
			return nil
		}
		rel, err := filepath.Rel(srcDir, path)
		if err != nil {
			return err
		}
		obj := dstPrefix + "/" + filepath.ToSlash(rel)
		// S3 object keys must be valid UTF-8; skip files with non-UTF-8
		// names so a single bad file does not fail the whole mirror.
		if !utf8.ValidString(obj) {
			return nil
		}
		if !opts.Overwrite {
			if _, err := c.client.StatObject(ctx, bucket, obj, minio.StatObjectOptions{}); err == nil {
				return nil
			}
		}
		_, err = c.client.FPutObject(ctx, bucket, obj, path, minio.PutObjectOptions{})
		return err
	})
}

// mirrorRemoteToRemote copies objects under one prefix to another. Not used by
// production callers (all Mirror sources are local directories), but keeps the
// driver behavior-compatible with MinIOClient, which also accepted remote
// sources.
func (c *SDKClient) mirrorRemoteToRemote(ctx context.Context, src, dst string, opts MirrorOptions) error {
	srcBucket, srcPrefix := c.bucketAndKey(src)
	dstBucket, dstPrefix := c.bucketAndKey(dst)
	for obj := range c.client.ListObjects(ctx, srcBucket, minio.ListObjectsOptions{Prefix: srcPrefix + "/", Recursive: true}) {
		if obj.Err != nil {
			return obj.Err
		}
		rel := strings.TrimPrefix(obj.Key, srcPrefix)
		dstObj := dstPrefix + "/" + strings.TrimPrefix(rel, "/")
		if !opts.Overwrite {
			if _, err := c.client.StatObject(ctx, dstBucket, dstObj, minio.StatObjectOptions{}); err == nil {
				continue
			}
		}
		rc, err := c.client.GetObject(ctx, srcBucket, obj.Key, minio.GetObjectOptions{})
		if err != nil {
			return err
		}
		data, readErr := io.ReadAll(rc)
		rc.Close()
		if readErr != nil {
			return readErr
		}
		if _, err := c.client.PutObject(ctx, dstBucket, dstObj, bytes.NewReader(data), int64(len(data)), minio.PutObjectOptions{}); err != nil {
			return err
		}
	}
	return nil
}

// EnsureBucket creates the configured bucket if it does not already exist
// (mc mb --ignore-existing semantics).
func (c *SDKClient) EnsureBucket(ctx context.Context) error {
	return c.timedOp(ctx, "ensurebucket", c.cfg.Bucket, func() error {
		exists, err := c.client.BucketExists(ctx, c.cfg.Bucket)
		if err != nil {
			return err
		}
		if exists {
			return nil
		}
		return c.client.MakeBucket(ctx, c.cfg.Bucket, minio.MakeBucketOptions{})
	})
}

// Compile-time interface satisfaction checks.
var (
	_ StorageClient = (*SDKClient)(nil)
	_ BucketManager = (*SDKClient)(nil)
)
