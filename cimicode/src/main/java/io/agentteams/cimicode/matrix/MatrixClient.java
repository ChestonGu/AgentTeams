package io.agentteams.cimicode.matrix;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import io.agentteams.cimicode.config.BridgeProperties;
import io.agentteams.cimicode.config.OpenClawConfigLoader;
import io.agentteams.cimicode.readiness.ReadinessState;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.boot.context.event.ApplicationReadyEvent;
import org.springframework.context.event.EventListener;
import org.springframework.stereotype.Component;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.concurrent.atomic.AtomicLong;

/**
 * Matrix 客户端：密码登录（P4 重登录语义）+ /sync 长轮询 + 邀请 join + 消息发送。
 *
 * 不实现 E2EE（架构决策：非加密房间）。token 只存内存，每次启动重新登录，
 * 与 hermes/copaw worker 的行为一致。
 */
@Component
public class MatrixClient {

    private static final Logger log = LoggerFactory.getLogger(MatrixClient.class);

    private final HttpClient http;
    private final ObjectMapper mapper;
    private final BridgeProperties props;
    private final OpenClawConfigLoader openClaw;
    private final ObjectProvider<MessageHandler> handlerProvider;
    private final ReadinessState readiness;

    private volatile String accessToken;
    private volatile String deviceId;
    private volatile String userId;
    private volatile String nextBatch;
    private volatile boolean running;
    private final AtomicLong txnCounter = new AtomicLong();

    public MatrixClient(HttpClient http,
                        ObjectMapper mapper,
                        BridgeProperties props,
                        OpenClawConfigLoader openClaw,
                        ObjectProvider<MessageHandler> handlerProvider,
                        ReadinessState readiness) {
        this.http = http;
        this.mapper = mapper;
        this.props = props;
        this.openClaw = openClaw;
        this.handlerProvider = handlerProvider;
        this.readiness = readiness;
    }

    @EventListener(ApplicationReadyEvent.class)
    public void start() {
        running = true;
        Thread.ofVirtual().name("matrix-login").start(this::loginAndSync);
    }

    public String userId() {
        return userId;
    }

    // ── 登录与 sync 循环 ───────────────────────────────────────────────

    private void loginAndSync() {
        try {
            login();
        } catch (Exception e) {
            log.error("matrix login failed: {}", e.getMessage());
            readiness.markMatrixFailed("login: " + e.getMessage());
            return;
        }
        readiness.markMatrixReady();
        while (running) {
            try {
                syncOnce();
            } catch (InterruptedException ie) {
                Thread.currentThread().interrupt();
                return;
            } catch (Exception e) {
                log.warn("sync error: {} -- retrying in 5s", e.getMessage());
                // 401 类错误重登录
                if (e instanceof MatrixAuthException) {
                    try {
                        login();
                    } catch (Exception le) {
                        log.error("re-login failed: {}", le.getMessage());
                    }
                }
                try {
                    Thread.sleep(5000);
                } catch (InterruptedException ie) {
                    Thread.currentThread().interrupt();
                    return;
                }
            }
        }
    }

    void login() throws Exception {
        var matrix = props.matrix();
        String homeserver = resolveHomeserver(matrix);
        String password = resolvePassword(matrix);
        String user = matrix.userId() != null && !matrix.userId().isBlank()
                ? matrix.userId()
                : matrix.localpart();

        ObjectNode body = mapper.createObjectNode();
        body.put("type", "m.login.password");
        ObjectNode identifier = body.putObject("identifier");
        identifier.put("type", "m.id.user");
        identifier.put("user", user);
        body.put("password", password);
        body.put("initial_device_display_name", "cimicode-bridge");

        HttpRequest request = HttpRequest.newBuilder(URI.create(homeserver + "/_matrix/client/v3/login"))
                .timeout(Duration.ofSeconds(30))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(mapper.writeValueAsString(body)))
                .build();
        HttpResponse<String> response = http.send(request, HttpResponse.BodyHandlers.ofString());
        if (response.statusCode() != 200) {
            throw new IllegalStateException("login HTTP " + response.statusCode() + ": " + response.body());
        }
        JsonNode json = mapper.readTree(response.body());
        this.accessToken = json.path("access_token").asText();
        this.deviceId = json.path("device_id").asText();
        this.userId = json.path("user_id").asText();
        if (this.accessToken == null || this.accessToken.isBlank()) {
            throw new IllegalStateException("login response missing access_token");
        }
        log.info("matrix login ok: {} (device {})", userId, deviceId);
    }

    private void syncOnce() throws Exception {
        var matrix = props.matrix();
        String homeserver = resolveHomeserver(matrix);
        StringBuilder url = new StringBuilder(homeserver).append("/_matrix/client/v3/sync?timeout=")
                .append(matrix.syncTimeoutMs());
        if (nextBatch != null) {
            url.append("&since=").append(nextBatch);
        }
        HttpRequest request = HttpRequest.newBuilder(URI.create(url.toString()))
                .timeout(Duration.ofMillis(matrix.syncTimeoutMs() + 30_000))
                .header("Authorization", "Bearer " + accessToken)
                .GET()
                .build();
        HttpResponse<String> response = http.send(request, HttpResponse.BodyHandlers.ofString());
        if (response.statusCode() == 401) {
            throw new MatrixAuthException("sync 401");
        }
        if (response.statusCode() == 429) {
            long waitMs = extractRetryAfterMs(response.body(), 5000);
            Thread.sleep(waitMs);
            return;
        }
        if (response.statusCode() != 200) {
            throw new IllegalStateException("sync HTTP " + response.statusCode());
        }
        SyncDtos.SyncResponse sync = mapper.readValue(response.body(), SyncDtos.SyncResponse.class);
        this.nextBatch = sync.nextBatch();

        var rooms = sync.rooms() == null ? null : sync.rooms();
        if (rooms != null && rooms.invite() != null) {
            rooms.invite().forEach((roomId, invited) -> acceptInvite(homeserver, roomId));
        }
        if (rooms != null && rooms.join() != null) {
            rooms.join().forEach((roomId, joined) -> dispatchTimeline(roomId, joined));
        }
    }

    private void acceptInvite(String homeserver, String roomId) {
        log.info("accepting invite for {}", roomId);
        try {
            HttpRequest request = HttpRequest.newBuilder(
                            URI.create(homeserver + "/_matrix/client/v3/join/" + urlEncode(roomId)))
                    .timeout(Duration.ofSeconds(30))
                    .header("Authorization", "Bearer " + accessToken)
                    .header("Content-Type", "application/json")
                    .POST(HttpRequest.BodyPublishers.ofString("{}"))
                    .build();
            HttpResponse<String> response = http.send(request, HttpResponse.BodyHandlers.ofString());
            if (response.statusCode() != 200 && response.statusCode() != 403) {
                log.warn("join {} HTTP {}: {}", roomId, response.statusCode(), response.body());
            }
        } catch (Exception e) {
            log.warn("join {} failed: {}", roomId, e.getMessage());
        }
    }

    private void dispatchTimeline(String roomId, SyncDtos.JoinedRoom joined) {
        if (joined == null || joined.timeline() == null || joined.timeline().events() == null) {
            return;
        }
        for (SyncDtos.Event event : joined.timeline().events()) {
            if (!"m.room.message".equals(event.type()) || event.content() == null) {
                continue;
            }
            String sender = event.sender();
            if (sender != null && sender.equals(this.userId)) {
                continue;
            }
            String body = event.content().path("body").asText("");
            if (body.isBlank()) {
                continue;
            }
            if (props.matrix().requireMention() && !isMentioned(event.content())) {
                continue;
            }
            String cleaned = stripMention(body);
            if (cleaned.isBlank()) {
                continue;
            }
            MessageHandler handler = handlerProvider.getIfAvailable();
            if (handler == null) {
                log.warn("no MessageHandler registered; dropping message in {}", roomId);
                continue;
            }
            final String fRoom = roomId;
            final String fSender = sender;
            final String fBody = cleaned;
            Thread.ofVirtual().name("msg-" + fRoom).start(() -> {
                try {
                    handler.handle(fRoom, fSender, fBody);
                } catch (Exception e) {
                    log.error("handler error in {}: {}", fRoom, e.getMessage(), e);
                }
            });
        }
    }

    private boolean isMentioned(JsonNode content) {
        JsonNode mentions = content.path("m.mentions").path("user_ids");
        if (mentions.isArray()) {
            for (JsonNode uid : mentions) {
                if (userId != null && userId.equals(uid.asText())) {
                    return true;
                }
            }
        }
        // 兜底：正文里出现完整 user ID 或 localpart
        String body = content.path("body").asText("");
        return (userId != null && body.contains(userId))
                || body.contains("@" + props.matrix().localpart() + ":")
                || body.contains("@" + props.matrix().localpart() + " ");
    }

    private String stripMention(String body) {
        if (userId != null) {
            body = body.replace(userId, "");
        }
        String localpart = props.matrix().localpart();
        if (localpart != null && !localpart.isBlank()) {
            body = body.replace("@" + localpart, "");
        }
        return body.trim();
    }

    // ── 发送 ──────────────────────────────────────────────────────────

    /**
     * 发送文本消息，回复时 mention 对端（保持 AgentTeams 房间的 @mention 唤醒协议）。
     */
    public void sendText(String roomId, String text, String mentionUserId) throws Exception {
        String homeserver = resolveHomeserver(props.matrix());
        ObjectNode content = mapper.createObjectNode();
        content.put("msgtype", "m.text");
        content.put("body", text);
        if (mentionUserId != null && !mentionUserId.isBlank()) {
            content.putObject("m.mentions").putArray("user_ids").add(mentionUserId);
        }
        String txnId = "bridge" + txnCounter.incrementAndGet();
        HttpRequest request = HttpRequest.newBuilder(
                        URI.create(homeserver + "/_matrix/client/v3/rooms/" + urlEncode(roomId)
                                + "/send/m.room.message/" + txnId))
                .timeout(Duration.ofSeconds(30))
                .header("Authorization", "Bearer " + accessToken)
                .header("Content-Type", "application/json")
                .PUT(HttpRequest.BodyPublishers.ofString(mapper.writeValueAsString(content)))
                .build();
        HttpResponse<String> response = http.send(request, HttpResponse.BodyHandlers.ofString());
        if (response.statusCode() == 429) {
            long waitMs = extractRetryAfterMs(response.body(), 2000);
            Thread.sleep(waitMs);
            response = http.send(request, HttpResponse.BodyHandlers.ofString());
        }
        if (response.statusCode() != 200) {
            throw new IllegalStateException("send HTTP " + response.statusCode() + ": " + response.body());
        }
    }

    // ── 工具 ──────────────────────────────────────────────────────────

    private String resolveHomeserver(BridgeProperties.Matrix matrix) {
        if (matrix.homeserverUrl() != null && !matrix.homeserverUrl().isBlank()) {
            return trimTrailingSlash(matrix.homeserverUrl());
        }
        return openClaw.loadMatrixUrl(props.workspace())
                .map(this::trimTrailingSlash)
                .orElseThrow(() -> new IllegalStateException(
                        "matrix homeserver URL not set (AGENTTEAMS_MATRIX_URL or openclaw.json)"));
    }

    private String resolvePassword(BridgeProperties.Matrix matrix) throws IOException {
        if (matrix.password() != null && !matrix.password().isBlank()) {
            return matrix.password();
        }
        Path file = Path.of(matrix.passwordFile());
        if (!Files.isReadable(file)) {
            throw new IllegalStateException("matrix password file not readable: " + file);
        }
        return Files.readString(file).trim();
    }

    private String trimTrailingSlash(String url) {
        return url.endsWith("/") ? url.substring(0, url.length() - 1) : url;
    }

    private long extractRetryAfterMs(String body, long defaultMs) {
        try {
            JsonNode json = mapper.readTree(body);
            long ms = json.path("retry_after_ms").asLong(0);
            if (ms <= 0) {
                ms = (long) (json.path("error").path("retry_after_ms").asDouble(0) * 1000);
            }
            return ms > 0 ? ms : defaultMs;
        } catch (Exception e) {
            return defaultMs;
        }
    }

    private static String urlEncode(String value) {
        return java.net.URLEncoder.encode(value, java.nio.charset.StandardCharsets.UTF_8);
    }

    static final class MatrixAuthException extends RuntimeException {
        MatrixAuthException(String message) {
            super(message);
        }
    }
}
