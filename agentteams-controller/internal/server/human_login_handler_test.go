package server

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	v1beta1 "github.com/agentscope-ai/AgentTeams/agentteams-controller/api/v1beta1"
	"github.com/agentscope-ai/AgentTeams/agentteams-controller/internal/service"
	"github.com/agentscope-ai/AgentTeams/agentteams-controller/test/testutil/mocks"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
)

func TestHumanLoginCreatesHumanAndReturnsProvisionedToken(t *testing.T) {
	scheme := newServerTestScheme(t)
	k8sClient := fake.NewClientBuilder().WithScheme(scheme).Build()
	provisioner := &mocks.MockHumanProvisioner{}
	handler := NewHumanLoginHandler(k8sClient, "default", "ctrl-a", provisioner)

	req := httptest.NewRequest(http.MethodPost, "/api/v1/humans/sso-login", bytes.NewBufferString(`{"name":"alice","initialPassword":"pw"}`))
	rec := httptest.NewRecorder()
	handler.Login(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	var response HumanLoginResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &response); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	if !response.Created || response.AccessToken == "" {
		t.Fatalf("response=%+v, want created response with token", response)
	}
	var human v1beta1.Human
	if err := k8sClient.Get(context.Background(), client.ObjectKey{Name: "alice", Namespace: "default"}, &human); err != nil {
		t.Fatalf("get Human: %v", err)
	}
	if human.Spec.InitialPassword != "pw" {
		t.Fatalf("initialPassword=%q, want pw", human.Spec.InitialPassword)
	}
	if human.Labels[v1beta1.LabelController] != "ctrl-a" {
		t.Fatalf("controller label=%q, want ctrl-a", human.Labels[v1beta1.LabelController])
	}
	if len(provisioner.Calls.EnsureHumanUser) != 1 {
		t.Fatalf("EnsureHumanUser calls=%d, want 1", len(provisioner.Calls.EnsureHumanUser))
	}
}

func TestHumanLoginExistingUsesDeviceID(t *testing.T) {
	scheme := newServerTestScheme(t)
	human := &v1beta1.Human{
		ObjectMeta: metav1.ObjectMeta{Name: "alice", Namespace: "default"},
		Spec:       v1beta1.HumanSpec{Username: "matrix-alice", InitialPassword: "pw"},
		Status:     v1beta1.HumanStatus{MatrixUserID: "@alice:localhost", Phase: "Pending"},
	}
	k8sClient := fake.NewClientBuilder().WithScheme(scheme).WithObjects(human).Build()
	provisioner := &mocks.MockHumanProvisioner{}
	provisioner.LoginWithPasswordAndOptionsFn = func(_ context.Context, username, password, deviceID string) (string, error) {
		if username != "matrix-alice" || password != "pw" || deviceID != "agi-teams-alice" {
			t.Fatalf("login args=(%q,%q,%q)", username, password, deviceID)
		}
		return "token", nil
	}
	handler := NewHumanLoginHandler(k8sClient, "default", "", provisioner)

	req := httptest.NewRequest(http.MethodPost, "/api/v1/humans/sso-login", bytes.NewBufferString(`{"name":"alice","initialPassword":"pw","deviceId":"agi-teams-alice"}`))
	rec := httptest.NewRecorder()
	handler.Login(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	if len(provisioner.Calls.LoginWithPasswordAndOptions) != 1 {
		t.Fatalf("option login calls=%d, want 1", len(provisioner.Calls.LoginWithPasswordAndOptions))
	}
	if len(provisioner.Calls.EnsureHumanUser) != 0 {
		t.Fatalf("EnsureHumanUser calls=%d, want 0", len(provisioner.Calls.EnsureHumanUser))
	}
}

func TestHumanLoginPasswordMismatchDoesNotLogin(t *testing.T) {
	scheme := newServerTestScheme(t)
	human := &v1beta1.Human{
		ObjectMeta: metav1.ObjectMeta{Name: "alice", Namespace: "default"},
		Spec:       v1beta1.HumanSpec{InitialPassword: "pw"},
	}
	k8sClient := fake.NewClientBuilder().WithScheme(scheme).WithObjects(human).Build()
	provisioner := &mocks.MockHumanProvisioner{}
	handler := NewHumanLoginHandler(k8sClient, "default", "", provisioner)

	req := httptest.NewRequest(http.MethodPost, "/api/v1/humans/sso-login", bytes.NewBufferString(`{"name":"alice","initialPassword":"wrong"}`))
	rec := httptest.NewRecorder()
	handler.Login(rec, req)

	if rec.Code != http.StatusConflict {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	if bytes.Contains(rec.Body.Bytes(), []byte("pw")) {
		t.Fatalf("response leaked pinned password: %s", rec.Body.String())
	}
	if len(provisioner.Calls.LoginWithPasswordAndOptions) != 0 {
		t.Fatalf("login calls=%d, want 0", len(provisioner.Calls.LoginWithPasswordAndOptions))
	}
}

func TestHumanLoginRejectsInvalidDeviceID(t *testing.T) {
	scheme := newServerTestScheme(t)
	k8sClient := fake.NewClientBuilder().WithScheme(scheme).Build()
	provisioner := &mocks.MockHumanProvisioner{}
	handler := NewHumanLoginHandler(k8sClient, "default", "", provisioner)

	req := httptest.NewRequest(http.MethodPost, "/api/v1/humans/sso-login", bytes.NewBufferString(`{"name":"alice","deviceId":"bad device"}`))
	rec := httptest.NewRecorder()
	handler.Login(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	var human v1beta1.Human
	if err := k8sClient.Get(context.Background(), client.ObjectKey{Name: "alice", Namespace: "default"}, &human); human.Name != "" || err == nil {
		t.Fatal("invalid device ID must not create a Human")
	}
}

func TestHumanLoginRejectsExistingExternalSSOHuman(t *testing.T) {
	scheme := newServerTestScheme(t)
	human := &v1beta1.Human{
		ObjectMeta: metav1.ObjectMeta{Name: "alice", Namespace: "default"},
		Spec: v1beta1.HumanSpec{
			IdentitySource:  &v1beta1.IdentitySourceSpec{Issuer: "https://issuer", Subject: "subject"},
			InitialPassword: "pw",
		},
	}
	k8sClient := fake.NewClientBuilder().WithScheme(scheme).WithObjects(human).Build()
	provisioner := &mocks.MockHumanProvisioner{}
	handler := NewHumanLoginHandler(k8sClient, "default", "", provisioner)

	req := httptest.NewRequest(http.MethodPost, "/api/v1/humans/sso-login", bytes.NewBufferString(`{"name":"alice"}`))
	rec := httptest.NewRecorder()
	handler.Login(rec, req)

	if rec.Code != http.StatusNotImplemented {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	if len(provisioner.Calls.LoginWithPasswordAndOptions) != 0 {
		t.Fatal("external SSO Human must not use password login")
	}
}

var _ service.HumanProvisioner = (*mocks.MockHumanProvisioner)(nil)
