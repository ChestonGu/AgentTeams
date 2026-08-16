package io.agentteams.cimicode.matrix;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.databind.JsonNode;

import java.util.List;
import java.util.Map;

/**
 * /sync 响应的最小解析模型（忽略所有未知字段，content 保持 JsonNode 按需读取）。
 */
public final class SyncDtos {

    private SyncDtos() {
    }

    public record SyncResponse(String nextBatch, Rooms rooms) {
    }

    public record Rooms(Map<String, JoinedRoom> join, Map<String, InvitedRoom> invite) {
    }

    public record JoinedRoom(Timeline timeline) {
    }

    public record InvitedRoom(InviteState inviteState) {
    }

    public record InviteState(List<StrippedState> events) {
    }

    public record StrippedState(String type, String sender, JsonNode content) {
    }

    public record Timeline(List<Event> events) {
    }

    public record Event(String type, String sender, JsonNode content) {
    }
}
