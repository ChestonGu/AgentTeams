package io.agentteams.cimicode.orchestration;

import java.time.Instant;

/**
 * 一个 Matrix 房间的编排状态（sessionID ↔ sandboxID 映射，规格 §4.2）。
 * 持久化于 workspace 的 bridge-state.json，Pod 重启后恢复。
 */
public record SessionState(
        String roomId,
        String sessionID,
        String sandboxID,
        String historyFile,
        String lastUsedAt) {

    public SessionState withUsedNow() {
        return new SessionState(roomId, sessionID, sandboxID, historyFile,
                Instant.now().toString());
    }

    public SessionState withNewSandbox(String sessionID, String sandboxID, String historyFile) {
        return new SessionState(roomId, sessionID, sandboxID, historyFile,
                Instant.now().toString());
    }
}
