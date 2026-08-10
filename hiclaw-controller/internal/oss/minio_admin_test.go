package oss

import (
	"errors"
	"fmt"
	"testing"

	"github.com/minio/madmin-go/v3"
)

func TestEndpointHost(t *testing.T) {
	cases := []struct {
		endpoint string
		host     string
		secure   bool
	}{
		{"http://minio:9000", "minio:9000", false},
		{"https://oss-cn-hangzhou.aliyuncs.com", "oss-cn-hangzhou.aliyuncs.com", true},
		{"minio:9000", "minio:9000", false},
		{"", "", false},
	}
	for _, tc := range cases {
		host, secure := endpointHost(tc.endpoint)
		if host != tc.host || secure != tc.secure {
			t.Errorf("endpointHost(%q) = (%q, %v), want (%q, %v)",
				tc.endpoint, host, secure, tc.host, tc.secure)
		}
	}
}

func TestNewSDKAdminClient_ParsesEndpoint(t *testing.T) {
	cli, err := NewSDKAdminClient(Config{
		Endpoint:  "http://minio:9000",
		AccessKey: "minioadmin",
		SecretKey: "minioadmin",
	})
	if err != nil {
		t.Fatalf("NewSDKAdminClient: %v", err)
	}
	u := cli.client.GetEndpointURL()
	if u.Scheme != "http" || u.Host != "minio:9000" {
		t.Errorf("endpoint = %v, want http://minio:9000", u)
	}
}

func TestNewMinIOAdminClient_Defaults(t *testing.T) {
	c := NewMinIOAdminClient(Config{})
	if c.config.MCBinary != "mc" {
		t.Errorf("MCBinary = %q, want mc", c.config.MCBinary)
	}
	if c.config.Alias != "hiclaw" {
		t.Errorf("Alias = %q, want hiclaw", c.config.Alias)
	}
}

func TestIsAdminAlreadyExists(t *testing.T) {
	userErr := madmin.ErrorResponse{Code: "UserAlreadyExists", Message: "Specified user already exists"}
	policyErr := madmin.ErrorResponse{Code: "PolicyAlreadyExists", Message: "Specified policy already exists"}
	otherErr := madmin.ErrorResponse{Code: "AccessDenied", Message: "Access Denied"}

	if !isAdminAlreadyExists(userErr) {
		t.Error("UserAlreadyExists should match")
	}
	if !isAdminAlreadyExists(policyErr) {
		t.Error("PolicyAlreadyExists should match")
	}
	if isAdminAlreadyExists(otherErr) {
		t.Error("AccessDenied should not match")
	}
	if isAdminAlreadyExists(nil) {
		t.Error("nil should not match")
	}
	// Substring fallback for servers without structured error codes.
	if !isAdminAlreadyExists(errors.New("user already exists")) {
		t.Error("message-substring fallback should match")
	}
	// Wrapped errors still match through the chain.
	if !isAdminAlreadyExists(fmt.Errorf("ensure minio user x: %w", userErr)) {
		t.Error("wrapped error should match")
	}
}

func TestIsAdminNotExists(t *testing.T) {
	userErr := madmin.ErrorResponse{Code: "NoSuchUser", Message: "The specified user does not exist"}
	policyErr := madmin.ErrorResponse{Code: "NoSuchPolicy", Message: "The specified policy does not exist"}
	otherErr := madmin.ErrorResponse{Code: "AccessDenied", Message: "Access Denied"}

	if !isAdminNotExists(userErr) {
		t.Error("NoSuchUser should match")
	}
	if !isAdminNotExists(policyErr) {
		t.Error("NoSuchPolicy should match")
	}
	if isAdminNotExists(otherErr) {
		t.Error("AccessDenied should not match")
	}
	if isAdminNotExists(nil) {
		t.Error("nil should not match")
	}
	if !isAdminNotExists(errors.New("the specified user does not exist")) {
		t.Error("message-substring fallback should match")
	}
	if !isAdminNotExists(fmt.Errorf("delete minio user x: %w", userErr)) {
		t.Error("wrapped error should match")
	}
}
