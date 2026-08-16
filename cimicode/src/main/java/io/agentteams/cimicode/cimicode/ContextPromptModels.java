package io.agentteams.cimicode.cimicode;

import com.fasterxml.jackson.annotation.JsonInclude;

import java.util.List;

/**
 * /session/context_prompt 请求体（与 cimicode ExportData 契约对齐，规格 wiki
 * cimicode-runtime-integration.md §3.3）。所有可选字段为 null 时不序列化。
 */
public final class ContextPromptModels {

    private ContextPromptModels() {
    }

    @JsonInclude(JsonInclude.Include.NON_NULL)
    public record ContextPromptRequest(
            ExportData context,
            List<Part> parts,
            String sessionID,
            String sandboxID,
            ModelRef model,
            String agent,
            String directory,
            String system,
            String variant) {
    }

    @JsonInclude(JsonInclude.Include.NON_NULL)
    public record ModelRef(String providerID, String modelID) {
    }

    /** 消息 part；v1 只用 text。 */
    @JsonInclude(JsonInclude.Include.NON_NULL)
    public record Part(String type, String text) {
        public static Part text(String text) {
            return new Part("text", text);
        }
    }

    @JsonInclude(JsonInclude.Include.NON_NULL)
    public record ExportData(SessionInfo info, List<MessageEntry> messages) {
    }

    @JsonInclude(JsonInclude.Include.NON_NULL)
    public record SessionInfo(String id, String title) {
    }

    @JsonInclude(JsonInclude.Include.NON_NULL)
    public record MessageEntry(MessageInfo info, List<Part> parts) {
    }

    @JsonInclude(JsonInclude.Include.NON_NULL)
    public record MessageInfo(String id, Long time, String role) {
    }
}
