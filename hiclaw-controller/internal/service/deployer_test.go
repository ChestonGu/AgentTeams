package service

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	v1beta1 "github.com/hiclaw/hiclaw-controller/api/v1beta1"
	"github.com/hiclaw/hiclaw-controller/internal/agentconfig"
	"github.com/hiclaw/hiclaw-controller/internal/oss/ossfake"
)

func TestDeployWorkerConfigSeedsLocalFilesWithoutOverwritingRuntimeState(t *testing.T) {
	ctx := context.Background()
	tmp := t.TempDir()
	agentFSDir := filepath.Join(tmp, "agents")
	workerDir := filepath.Join(agentFSDir, "alice")
	if err := os.MkdirAll(filepath.Join(workerDir, "config"), 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(workerDir, "config", "credagent.json"), []byte(`{"source":"template"}`), 0644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(workerDir, "notes.md"), []byte("template note"), 0644); err != nil {
		t.Fatal(err)
	}

	store := ossfake.NewMemory()
	if err := store.PutObject(ctx, "agents/alice/config/credagent.json", []byte(`{"source":"runtime"}`)); err != nil {
		t.Fatal(err)
	}
	if err := store.PutObject(ctx, "agents/alice/openclaw.json", []byte(`{"old":true}`)); err != nil {
		t.Fatal(err)
	}

	deployer := NewDeployer(DeployerConfig{
		AgentConfig: agentconfig.NewGenerator(agentconfig.Config{}),
		OSS:         store,
		AgentFSDir:  agentFSDir,
	})
	err := deployer.DeployWorkerConfig(ctx, WorkerDeployRequest{
		Name:        "alice",
		MatrixToken: "matrix-token",
		GatewayKey:  "gateway-key",
	})
	if err != nil {
		t.Fatalf("DeployWorkerConfig failed: %v", err)
	}

	got, err := store.GetObject(ctx, "agents/alice/config/credagent.json")
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != `{"source":"runtime"}` {
		t.Fatalf("credagent.json overwritten: %s", got)
	}

	got, err = store.GetObject(ctx, "agents/alice/notes.md")
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != "template note" {
		t.Fatalf("notes.md not seeded: %s", got)
	}

	got, err = store.GetObject(ctx, "agents/alice/openclaw.json")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(got), "gateway-key") {
		t.Fatalf("openclaw.json was not overwritten by controller config: %s", got)
	}
}

func TestDeployWorkerConfigInlineSoulOverridesPackageSeed(t *testing.T) {
	ctx := context.Background()
	tmp := t.TempDir()
	agentFSDir := filepath.Join(tmp, "agents")
	workerDir := filepath.Join(agentFSDir, "alice")
	if err := os.MkdirAll(workerDir, 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(workerDir, "SOUL.md"), []byte("OVERRIDDEN SOUL FROM INLINE\n"), 0644); err != nil {
		t.Fatal(err)
	}

	store := ossfake.NewMemory()
	if err := store.PutObject(ctx, "agents/alice/SOUL.md", []byte("ORIGINAL SOUL FROM PACKAGE\n")); err != nil {
		t.Fatal(err)
	}

	deployer := NewDeployer(DeployerConfig{
		AgentConfig: agentconfig.NewGenerator(agentconfig.Config{}),
		OSS:         store,
		AgentFSDir:  agentFSDir,
	})
	err := deployer.DeployWorkerConfig(ctx, WorkerDeployRequest{
		Name:        "alice",
		MatrixToken: "matrix-token",
		GatewayKey:  "gateway-key",
		IsUpdate:    true,
		Spec: v1beta1.WorkerSpec{
			Runtime: "hermes",
			Soul:    "OVERRIDDEN SOUL FROM INLINE",
		},
	})
	if err != nil {
		t.Fatalf("DeployWorkerConfig failed: %v", err)
	}

	got, err := store.GetObject(ctx, "agents/alice/SOUL.md")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(got), "OVERRIDDEN SOUL FROM INLINE") {
		t.Fatalf("SOUL.md did not contain inline content: %s", got)
	}
	if strings.Contains(string(got), "ORIGINAL SOUL FROM PACKAGE") {
		t.Fatalf("SOUL.md still contains package seed content: %s", got)
	}
}

func TestPushBuiltinSkillsContentCompare(t *testing.T) {
	ctx := context.Background()
	tmp := t.TempDir()
	workerAgentDir := filepath.Join(tmp, "worker-agent")
	skillDir := filepath.Join(workerAgentDir, "skills", "file-sync")
	if err := os.MkdirAll(skillDir, 0755); err != nil {
		t.Fatal(err)
	}
	skillMD := filepath.Join(skillDir, "SKILL.md")
	if err := os.WriteFile(skillMD, []byte("v1 content\n"), 0644); err != nil {
		t.Fatal(err)
	}

	store := ossfake.NewMemory()
	deployer := NewDeployer(DeployerConfig{
		OSS:            store,
		AgentFSDir:     filepath.Join(tmp, "agents"),
		WorkerAgentDir: workerAgentDir,
	})

	if err := deployer.pushBuiltinSkills(ctx, "alice", "agents/alice", "worker", "openclaw"); err != nil {
		t.Fatalf("first push failed: %v", err)
	}
	got, err := store.GetObject(ctx, "agents/alice/skills/file-sync/SKILL.md")
	if err != nil {
		t.Fatalf("skill not pushed: %v", err)
	}
	if string(got) != "v1 content\n" {
		t.Fatalf("unexpected skill content: %q", got)
	}

	// Unchanged second pass: content must be preserved.
	if err := deployer.pushBuiltinSkills(ctx, "alice", "agents/alice", "worker", "openclaw"); err != nil {
		t.Fatalf("second push failed: %v", err)
	}
	got, err = store.GetObject(ctx, "agents/alice/skills/file-sync/SKILL.md")
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != "v1 content\n" {
		t.Fatalf("unchanged push altered content: %q", got)
	}

	// Content update must propagate (unlike seed-only semantics).
	if err := os.WriteFile(skillMD, []byte("v2 content\n"), 0644); err != nil {
		t.Fatal(err)
	}
	if err := deployer.pushBuiltinSkills(ctx, "alice", "agents/alice", "worker", "openclaw"); err != nil {
		t.Fatalf("update push failed: %v", err)
	}
	got, err = store.GetObject(ctx, "agents/alice/skills/file-sync/SKILL.md")
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != "v2 content\n" {
		t.Fatalf("content update not propagated: %q", got)
	}
}

func TestPushOnDemandSkillsSkipsLocalPushByDefault(t *testing.T) {
	ctx := context.Background()
	store := ossfake.NewMemory()
	deployer := NewDeployer(DeployerConfig{
		OSS: store,
		// Executor left nil on purpose: the local push must not be reached.
	})

	if err := deployer.PushOnDemandSkills(ctx, "alice", []string{"github-operations"}, nil); err != nil {
		t.Fatalf("PushOnDemandSkills failed: %v", err)
	}
	names, err := store.ListObjects(ctx, "agents/alice/skills/")
	if err != nil {
		t.Fatal(err)
	}
	if len(names) != 0 {
		t.Fatalf("local skill push ran despite default-off switch: %v", names)
	}
}

func TestProbeStorage(t *testing.T) {
	ctx := context.Background()
	store := ossfake.NewMemory()

	deployer := NewDeployer(DeployerConfig{OSS: store})

	// Object missing → endpoint answered, healthy.
	if err := deployer.probeStorage(ctx, "agents/alice"); err != nil {
		t.Fatalf("probe with missing object failed: %v", err)
	}

	// Object present → healthy.
	if err := store.PutObject(ctx, "agents/alice/openclaw.json", []byte(`{}`)); err != nil {
		t.Fatal(err)
	}
	if err := deployer.probeStorage(ctx, "agents/alice"); err != nil {
		t.Fatalf("probe with existing object failed: %v", err)
	}

	// Network-class failure → probe surfaces the error for a fast abort.
	failing := &failingGetStorage{Memory: ossfake.NewMemory()}
	d2 := NewDeployer(DeployerConfig{OSS: failing})
	if err := d2.probeStorage(ctx, "agents/alice"); err == nil {
		t.Fatal("probe did not surface storage failure")
	}
}

// failingGetStorage wraps ossfake.Memory and fails every GetObject with a
// transport-class error, simulating an unreachable storage endpoint.
type failingGetStorage struct {
	*ossfake.Memory
}

func (f *failingGetStorage) GetObject(_ context.Context, _ string) ([]byte, error) {
	return nil, errors.New("dial tcp 10.0.0.1:9000: i/o timeout")
}
