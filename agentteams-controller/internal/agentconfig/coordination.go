package agentconfig

import (
	"fmt"
	"strings"
)

// CoordinationContext describes the team/coordination context to inject into AGENTS.md.
type CoordinationContext struct {
	WorkerName     string
	Role           string // "worker", "team_leader", "standalone"
	MatrixDomain   string
	TeamName       string
	TeamLeaderName string
	TeamAdminID    string // full Matrix ID of team admin
	// TeamAdminDisplayName is the admin's friendly name; empty renders the
	// bare admin Matrix ID (legacy behavior).
	TeamAdminDisplayName string
	TeamCoordinators     []TeamCoordinatorInfo
	TeamRoomID           string
	LeaderDMRoomID       string
	HeartbeatEvery       string
	WorkerIdleTimeout    string
	TeamWorkers          []TeamWorkerInfo // for leaders: list of team workers
}

// TeamCoordinatorInfo describes one human coordinator member for the
// coordination block, keeping the display name next to the Matrix ID so
// rendering can prefer the friendly name.
type TeamCoordinatorInfo struct {
	MatrixUserID string
	DisplayName  string // can be empty
}

// TeamWorkerInfo describes a team worker for leader context injection.
type TeamWorkerInfo struct {
	Name   string
	RoomID string
	// DisplayName is the friendly name for this worker (can be empty).
	DisplayName string
	// MatrixUserID is the member's Matrix MXID, if known.
	MatrixUserID string
	// Description is the member's spec.description: what this worker does
	// (can be empty). Rendered after the room so the leader can match task
	// decomposition to worker responsibilities.
	Description string
}

const (
	teamCtxStart = "<!-- agentteams-team-context-start -->"
	teamCtxEnd   = "<!-- agentteams-team-context-end -->"
)

// InjectCoordinationContext inserts the team-context block into AGENTS.md content.
// It replaces any existing team-context block, or appends after the builtin-end marker.
func InjectCoordinationContext(agentsContent string, ctx CoordinationContext) string {
	block := buildCoordinationBlock(ctx)
	cleaned := removeCoordinationBlock(agentsContent)

	if strings.Contains(cleaned, BuiltinEnd) {
		idx := strings.LastIndex(cleaned, BuiltinEnd)
		before := cleaned[:idx+len(BuiltinEnd)]
		after := cleaned[idx+len(BuiltinEnd):]
		return before + "\n" + block + after
	}

	return strings.TrimRight(cleaned, "\n") + "\n" + block
}

func buildCoordinationBlock(ctx CoordinationContext) string {
	var b strings.Builder
	b.WriteString("\n")
	b.WriteString(teamCtxStart)
	b.WriteString("\n## Coordination\n\n")

	switch ctx.Role {
	case "team_leader":
		fmt.Fprintf(&b, "- **Upstream coordinator**: @manager:%s (Manager) — you receive tasks from Manager\n", ctx.MatrixDomain)
		if ctx.TeamAdminID != "" {
			writeTeamAdmin(&b, ctx.TeamAdminID, ctx.TeamAdminDisplayName)
		}
		writeTeamCoordinators(&b, ctx.TeamCoordinators, ctx.TeamAdminID)
		fmt.Fprintf(&b, "- **Team**: %s\n", ctx.TeamName)
		if ctx.TeamRoomID != "" {
			fmt.Fprintf(&b, "- **Team Room**: %s — @mention workers here for task assignment\n", ctx.TeamRoomID)
		}
		if ctx.LeaderDMRoomID != "" {
			fmt.Fprintf(&b, "- **Leader DM**: %s — Team Admin communicates with you here\n", ctx.LeaderDMRoomID)
		}
		if ctx.HeartbeatEvery != "" {
			fmt.Fprintf(&b, "- **Heartbeat interval**: %s\n", ctx.HeartbeatEvery)
		}
		if ctx.WorkerIdleTimeout != "" {
			fmt.Fprintf(&b, "- **Worker idle timeout**: %s\n", ctx.WorkerIdleTimeout)
		}
		if len(ctx.TeamWorkers) > 0 {
			b.WriteString("- **Team Workers**:\n")
			for _, w := range ctx.TeamWorkers {
				roomInfo := "unknown"
				if w.RoomID != "" {
					roomInfo = w.RoomID
				}
				// Prefer showing friendly display name when available; append
				// the worker's responsibility description when set.
				desc := ""
				if w.Description != "" {
					desc = " — " + w.Description
				}
				if w.DisplayName != "" && w.DisplayName != w.Name {
					fmt.Fprintf(&b, "  - %s (@%s:%s) — Room: %s%s\n", w.DisplayName, w.Name, ctx.MatrixDomain, roomInfo, desc)
				} else {
					fmt.Fprintf(&b, "  - @%s:%s — Room: %s%s\n", w.Name, ctx.MatrixDomain, roomInfo, desc)
				}
			}
		}
		b.WriteString("- You decompose tasks from Manager, Team Admin, or coordinator members and assign sub-tasks to your team workers\n")
		b.WriteString("- @mention workers in the Team Room for task assignment\n")
		b.WriteString("- This Coordination block is already loaded into your system prompt; use these room IDs and worker Matrix IDs directly, without narrating topology checks or AGENTS.md reads\n")
		b.WriteString("- Use team-state.json as the source of truth for task activity before deciding whether a worker is idle\n")
		b.WriteString("- You decide when to wake or sleep team workers; the controller only executes the lifecycle action you request\n")
		b.WriteString("- Report results to Manager (in Leader Room) or Team Admin (in Leader DM) based on task source\n")
		b.WriteString("- @mention Manager only for: task completion, blockers, escalations\n")

	case "worker":
		fmt.Fprintf(&b, "- **Coordinator**: @%s:%s (Team Leader of %s)\n", ctx.TeamLeaderName, ctx.MatrixDomain, ctx.TeamName)
		if ctx.TeamAdminID != "" {
			writeTeamAdminWorkerView(&b, ctx.TeamAdminID, ctx.TeamAdminDisplayName)
		}
		writeTeamCoordinators(&b, ctx.TeamCoordinators, ctx.TeamAdminID)
		b.WriteString("- Report task completion, blockers, and questions to your coordinator\n")
		switch {
		case ctx.TeamAdminID != "" && len(ctx.TeamCoordinators) > 0:
			b.WriteString("- Respond to @mentions from your coordinator, Team Admin, coordinator members, and global Admin\n")
		case ctx.TeamAdminID != "":
			b.WriteString("- Respond to @mentions from your coordinator, Team Admin, and global Admin\n")
		case len(ctx.TeamCoordinators) > 0:
			b.WriteString("- Respond to @mentions from your coordinator, coordinator members, and global Admin\n")
		default:
			b.WriteString("- Respond to @mentions from your coordinator and global Admin\n")
		}
		b.WriteString("- Do NOT @mention Manager directly — all communication goes through your Team Leader\n")

	default: // standalone
		fmt.Fprintf(&b, "- **Coordinator**: @manager:%s (Manager)\n", ctx.MatrixDomain)
		b.WriteString("- Report task completion, blockers, and questions to your coordinator\n")
		b.WriteString("- Only respond to @mentions from your coordinator and Admin\n")
	}

	b.WriteString(teamCtxEnd)
	b.WriteString("\n")
	return b.String()
}

// writeTeamAdmin renders the leader-view Team Admin line, preferring the
// friendly display name with the Matrix ID in parentheses (mirrors the
// TeamWorkers rendering); a bare ID keeps the legacy format.
func writeTeamAdmin(b *strings.Builder, id, displayName string) {
	if displayName != "" && displayName != id {
		fmt.Fprintf(b, "- **Team Admin**: %s (%s) — can assign tasks and make decisions within the team\n", displayName, id)
		return
	}
	fmt.Fprintf(b, "- **Team Admin**: %s — can assign tasks and make decisions within the team\n", id)
}

// writeTeamAdminWorkerView renders the worker-view Team Admin line (same
// name-first policy as writeTeamAdmin).
func writeTeamAdminWorkerView(b *strings.Builder, id, displayName string) {
	if displayName != "" && displayName != id {
		fmt.Fprintf(b, "- **Team Admin**: %s (%s) (has admin authority within this team)\n", displayName, id)
		return
	}
	fmt.Fprintf(b, "- **Team Admin**: %s (has admin authority within this team)\n", id)
}

func writeTeamCoordinators(b *strings.Builder, members []TeamCoordinatorInfo, adminID string) {
	members = uniqueCoordinationMembers(members, adminID)
	if len(members) == 0 {
		return
	}
	b.WriteString("- **Coordinator Members**:\n")
	for _, m := range members {
		if m.DisplayName != "" && m.DisplayName != m.MatrixUserID {
			fmt.Fprintf(b, "  - %s (%s) — can assign tasks and make decisions within the team\n", m.DisplayName, m.MatrixUserID)
		} else {
			fmt.Fprintf(b, "  - %s — can assign tasks and make decisions within the team\n", m.MatrixUserID)
		}
	}
}

func uniqueCoordinationMembers(members []TeamCoordinatorInfo, exclude string) []TeamCoordinatorInfo {
	seen := make(map[string]struct{}, len(members))
	out := make([]TeamCoordinatorInfo, 0, len(members))
	for _, m := range members {
		if m.MatrixUserID == "" || m.MatrixUserID == exclude {
			continue
		}
		if _, ok := seen[m.MatrixUserID]; ok {
			continue
		}
		seen[m.MatrixUserID] = struct{}{}
		out = append(out, m)
	}
	return out
}

func removeCoordinationBlock(content string) string {
	for {
		startIdx := strings.Index(content, teamCtxStart)
		if startIdx < 0 {
			break
		}
		endIdx := strings.Index(content[startIdx:], teamCtxEnd)
		if endIdx < 0 {
			break
		}
		endIdx += startIdx + len(teamCtxEnd)
		// Consume trailing newline
		if endIdx < len(content) && content[endIdx] == '\n' {
			endIdx++
		}
		content = content[:startIdx] + content[endIdx:]
	}
	return content
}
