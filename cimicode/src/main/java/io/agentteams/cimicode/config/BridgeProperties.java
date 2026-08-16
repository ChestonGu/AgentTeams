package io.agentteams.cimicode.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * 桥配置。所有键从 AGENTTEAMS_*/BRIDGE_* 环境变量映射（见 application.yml），
 * 与 AgentTeams controller 注入的 env 契约对齐。
 */
@ConfigurationProperties(prefix = "bridge")
public record BridgeProperties(
        String workspace,
        String workerName,
        Matrix matrix,
        Cimicode cimicode,
        Sandbox sandbox,
        Session session,
        Reply reply,
        Readiness readiness) {

    public record Matrix(
            String homeserverUrl,
            String domain,
            String userId,
            String localpart,
            String password,
            String passwordFile,
            boolean requireMention,
            long syncTimeoutMs) {
    }

    public record Cimicode(
            String baseUrl,
            long requestTimeoutMs,
            int maxRetries) {
    }

    public record Sandbox(
            String baseUrl,
            String template,
            long ttlSeconds,
            long idlePauseSeconds,
            boolean enabled) {
    }

    public record Session(
            String stateFile,
            String historyDir,
            int maxHistoryMessages) {
    }

    public record Reply(
            int maxChunkChars,
            int maxChunks) {
    }

    public record Readiness(
            boolean enabled,
            String agtPath,
            long timeoutSeconds) {
    }
}
