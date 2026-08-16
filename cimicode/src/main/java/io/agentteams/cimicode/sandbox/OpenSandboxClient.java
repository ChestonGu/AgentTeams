package io.agentteams.cimicode.sandbox;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import io.agentteams.cimicode.config.BridgeProperties;
import io.agentteams.cimicode.readiness.ReadinessState;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.context.event.ApplicationReadyEvent;
import org.springframework.context.event.EventListener;
import org.springframework.stereotype.Component;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;

/**
 * OpenSandbox Server API 客户端（仅"使用"职责，v2 插件同款边界）：
 * create / get(state) / pause / resume / renewTTL / delete。
 *
 * ⚠️ 路径与响应字段按 cimicode 设计文档实现（/v1/sandboxes/*），
 * 与 OpenSandbox 实际部署联调时需核对（wiki spec Phase 0 待确认项）。
 */
@Component
public class OpenSandboxClient {

    private static final Logger log = LoggerFactory.getLogger(OpenSandboxClient.class);

    private final HttpClient http;
    private final ObjectMapper mapper;
    private final BridgeProperties props;
    private final ReadinessState readiness;

    public OpenSandboxClient(HttpClient http, ObjectMapper mapper, BridgeProperties props,
                             ReadinessState readiness) {
        this.http = http;
        this.mapper = mapper;
        this.props = props;
        this.readiness = readiness;
    }

    /** 沙箱状态。terminal = 已删除/失败（不可 resume，只能重建）。 */
    public enum SandboxState { RUNNING, PAUSED, PAUSING, TERMINAL, UNKNOWN }

    public record SandboxInfo(String id, SandboxState state) {
    }

    @EventListener(ApplicationReadyEvent.class)
    public void probe() {
        if (!props.sandbox().enabled()) {
            readiness.markSandboxReady();
            return;
        }
        Thread.ofVirtual().name("sandbox-probe").start(() -> {
            try {
                // 列一次沙箱：任何 HTTP 响应（含 401/403/404）都证明可达
                exchange("GET", "/v1/sandboxes", null, null);
                readiness.markSandboxReady();
                log.info("sandbox server reachable: {}", props.sandbox().baseUrl());
            } catch (Exception e) {
                readiness.markSandboxFailed(e.getMessage());
                log.warn("sandbox server unreachable: {}", e.getMessage());
            }
        });
    }

    public String create() throws SandboxException {
        try {
            ObjectNode body = mapper.createObjectNode();
            body.put("template", props.sandbox().template());
            body.put("ttlSeconds", props.sandbox().ttlSeconds());
            HttpResponse<String> response = exchange("POST", "/v1/sandboxes",
                    mapper.writeValueAsString(body), null);
            if (response.statusCode() / 100 != 2) {
                throw new SandboxException("create HTTP " + response.statusCode() + ": " + response.body());
            }
            JsonNode json = mapper.readTree(response.body());
            String id = firstNonBlank(json.path("id").asText(), json.path("sandboxID").asText());
            if (id == null) {
                throw new SandboxException("create response missing id: " + response.body());
            }
            log.info("sandbox created: {}", id);
            return id;
        } catch (SandboxException e) {
            throw e;
        } catch (Exception e) {
            throw new SandboxException("create failed: " + e.getMessage(), e);
        }
    }

    public SandboxInfo get(String sandboxId) throws SandboxException {
        try {
            HttpResponse<String> response = exchange("GET", "/v1/sandboxes/" + sandboxId, null, null);
            if (response.statusCode() == 404) {
                return new SandboxInfo(sandboxId, SandboxState.TERMINAL);
            }
            if (response.statusCode() / 100 != 2) {
                throw new SandboxException("get HTTP " + response.statusCode() + ": " + response.body());
            }
            JsonNode json = mapper.readTree(response.body());
            String state = firstNonBlank(json.path("state").asText(), json.path("status").asText(), "");
            return new SandboxInfo(sandboxId, mapState(state));
        } catch (SandboxException e) {
            throw e;
        } catch (Exception e) {
            throw new SandboxException("get failed: " + e.getMessage(), e);
        }
    }

    public void pause(String sandboxId) throws SandboxException {
        action(sandboxId, "pause");
    }

    public void resume(String sandboxId) throws SandboxException {
        action(sandboxId, "resume");
    }

    public void delete(String sandboxId) throws SandboxException {
        try {
            HttpResponse<String> response = exchange("DELETE", "/v1/sandboxes/" + sandboxId, null, null);
            if (response.statusCode() / 100 != 2 && response.statusCode() != 404) {
                throw new SandboxException("delete HTTP " + response.statusCode());
            }
            log.info("sandbox deleted: {}", sandboxId);
        } catch (SandboxException e) {
            throw e;
        } catch (Exception e) {
            throw new SandboxException("delete failed: " + e.getMessage(), e);
        }
    }

    /** 续期 TTL（R6：失败仅告警不中断，与插件同策略）。 */
    public void renewTtl(String sandboxId) {
        try {
            ObjectNode body = mapper.createObjectNode();
            body.put("ttlSeconds", props.sandbox().ttlSeconds());
            HttpResponse<String> response = exchange("POST",
                    "/v1/sandboxes/" + sandboxId + "/renewTTL",
                    mapper.writeValueAsString(body), null);
            if (response.statusCode() / 100 != 2) {
                log.warn("renewTTL {} HTTP {}: {}", sandboxId, response.statusCode(), response.body());
            }
        } catch (Exception e) {
            log.warn("renewTTL {} failed: {}", sandboxId, e.getMessage());
        }
    }

    private void action(String sandboxId, String action) throws SandboxException {
        try {
            HttpResponse<String> response = exchange("POST",
                    "/v1/sandboxes/" + sandboxId + "/" + action, "{}", null);
            if (response.statusCode() / 100 != 2) {
                throw new SandboxException(action + " HTTP " + response.statusCode() + ": " + response.body());
            }
            log.info("sandbox {} -> {}", action, sandboxId);
        } catch (SandboxException e) {
            throw e;
        } catch (Exception e) {
            throw new SandboxException(action + " failed: " + e.getMessage(), e);
        }
    }

    private HttpResponse<String> exchange(String method, String path, String jsonBody, String authToken)
            throws Exception {
        HttpRequest.Builder builder = HttpRequest.newBuilder(
                        URI.create(trimSlash(props.sandbox().baseUrl()) + path))
                .timeout(Duration.ofSeconds(30))
                .header("Content-Type", "application/json");
        if (authToken != null) {
            builder.header("Authorization", "Bearer " + authToken);
        }
        builder.method(method, jsonBody == null
                ? HttpRequest.BodyPublishers.noBody()
                : HttpRequest.BodyPublishers.ofString(jsonBody));
        return http.send(builder.build(), HttpResponse.BodyHandlers.ofString());
    }

    private SandboxState mapState(String state) {
        if (state == null || state.isBlank()) {
            return SandboxState.UNKNOWN;
        }
        return switch (state.toLowerCase()) {
            case "running" -> SandboxState.RUNNING;
            case "paused" -> SandboxState.PAUSED;
            case "pausing" -> SandboxState.PAUSING;
            case "deleted", "stopped", "failed", "error", "expired" -> SandboxState.TERMINAL;
            default -> SandboxState.UNKNOWN;
        };
    }

    private static String firstNonBlank(String... values) {
        for (String v : values) {
            if (v != null && !v.isBlank()) {
                return v;
            }
        }
        return null;
    }

    private static String trimSlash(String url) {
        return url.endsWith("/") ? url.substring(0, url.length() - 1) : url;
    }

    public static final class SandboxException extends Exception {
        public SandboxException(String message) {
            super(message);
        }

        public SandboxException(String message, Throwable cause) {
            super(message, cause);
        }
    }
}
