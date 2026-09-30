package agentconfig

import (
	"strings"
	"testing"
)

func TestCoordinationBlockLeaderShowsHumanNames(t *testing.T) {
	ctx := CoordinationContext{
		WorkerName:           "lead",
		Role:                 "team_leader",
		MatrixDomain:         "matrix.local",
		TeamName:             "alpha",
		TeamRoomID:           "!room:matrix.local",
		TeamAdminID:          "@admin:matrix.local",
		TeamAdminDisplayName: "Alice",
		TeamCoordinators: []TeamCoordinatorInfo{
			{MatrixUserID: "@admin:matrix.local", DisplayName: "Alice"}, // excluded: duplicates admin
			{MatrixUserID: "@bob:matrix.local", DisplayName: "Bob"},
			{MatrixUserID: "@carol:matrix.local"}, // no display name -> bare ID
		},
	}
	got := buildCoordinationBlock(ctx)

	wantNamedAdmin := "- **Team Admin**: Alice (@admin:matrix.local) — can assign tasks and make decisions within the team"
	if !strings.Contains(got, wantNamedAdmin) {
		t.Errorf("leader block missing named admin line:\nwant: %s\ngot:\n%s", wantNamedAdmin, got)
	}
	wantNamedCoord := "  - Bob (@bob:matrix.local) — can assign tasks and make decisions within the team"
	if !strings.Contains(got, wantNamedCoord) {
		t.Errorf("leader block missing named coordinator line:\nwant: %s\ngot:\n%s", wantNamedCoord, got)
	}
	wantBareCoord := "  - @carol:matrix.local — can assign tasks and make decisions within the team"
	if !strings.Contains(got, wantBareCoord) {
		t.Errorf("leader block missing bare coordinator line:\nwant: %s\ngot:\n%s", wantBareCoord, got)
	}
	// The admin-duplicate coordinator entry must be excluded from the
	// Coordinator Members list (admin already has its own line).
	adminAsCoordCount := strings.Count(got, "Alice (@admin:matrix.local)")
	if adminAsCoordCount != 1 {
		t.Errorf("admin should render exactly once, got %d occurrences of named admin:\n%s", adminAsCoordCount, got)
	}
}

func TestCoordinationBlockLeaderFallsBackToBareAdminID(t *testing.T) {
	ctx := CoordinationContext{
		WorkerName:   "lead",
		Role:         "team_leader",
		MatrixDomain: "matrix.local",
		TeamName:     "alpha",
		TeamAdminID:  "@admin:matrix.local",
	}
	got := buildCoordinationBlock(ctx)
	want := "- **Team Admin**: @admin:matrix.local — can assign tasks and make decisions within the team"
	if !strings.Contains(got, want) {
		t.Errorf("empty display name should keep legacy bare admin line:\nwant: %s\ngot:\n%s", want, got)
	}
}

func TestCoordinationBlockWorkerShowsHumanNames(t *testing.T) {
	ctx := CoordinationContext{
		WorkerName:           "w1",
		Role:                 "worker",
		MatrixDomain:         "matrix.local",
		TeamName:             "alpha",
		TeamLeaderName:       "lead",
		TeamAdminID:          "@admin:matrix.local",
		TeamAdminDisplayName: "Alice",
		TeamCoordinators: []TeamCoordinatorInfo{
			{MatrixUserID: "@admin:matrix.local", DisplayName: "Alice"},
			{MatrixUserID: "@bob:matrix.local", DisplayName: "Bob"},
		},
	}
	got := buildCoordinationBlock(ctx)
	want := "- **Team Admin**: Alice (@admin:matrix.local) (has admin authority within this team)"
	if !strings.Contains(got, want) {
		t.Errorf("worker block missing named admin line:\nwant: %s\ngot:\n%s", want, got)
	}
	wantCoord := "  - Bob (@bob:matrix.local) — can assign tasks and make decisions within the team"
	if !strings.Contains(got, wantCoord) {
		t.Errorf("worker block missing named coordinator line:\nwant: %s\ngot:\n%s", wantCoord, got)
	}
	// The admin/coordinator mention-policy line still counts both sources.
	wantPolicy := "- Respond to @mentions from your coordinator, Team Admin, coordinator members, and global Admin"
	if !strings.Contains(got, wantPolicy) {
		t.Errorf("worker block mention policy changed unexpectedly:\ngot:\n%s", got)
	}
}
