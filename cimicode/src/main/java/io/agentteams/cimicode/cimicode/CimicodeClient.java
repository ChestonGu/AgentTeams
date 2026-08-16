package io.agentteams.cimicode.cimicode;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import io.agentteams.cimicode.config.BridgeProperties;
import io.agentteams.cimicode.readiness.ReadinessState;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.context.event.ApplicationReadyEvent;
import org.springframework.context.event.EventListener;
import org.springframework.stereotype.Component;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.time.Instant;

/**
 * cimicode 核心 HTTP 客户端：POST /session/context_prompt（SSE 模式）。
 *
 * 实现约束（规格 §3.3）：
 * - 必须用 fetch/流式读取（这里是 JDK HttpClient ofInputStream）而非 EventSource 语义；
 * - 总时长受 request-timeout-ms 约束（heartbeat 10s 保活，正常不应触顶）；
 * - done 之前流中断 => onInterrupted（编排层负责 R1 全量重放）。
 */
@Component
public class CimicodeClient {

    private static final Logger log = LoggerFactory.getLogger(CimicodeClient.class);

    private final HttpClient http;
    private final ObjectMapper mapper;
    private final BridgeProperties props;
    private final ReadinessState readiness;

    public CimicodeClient(HttpClient http, ObjectMapper mapper, BridgeProperties props,
                          ReadinessState readiness) {
        this.http = http;
        this.mapper = mapper;
        this.props = props;
        this.readiness = readiness;
    }

    @EventListener(ApplicationReadyEvent.class)
    public void probe() {
        Thread.ofVirtual().name("cimicode-probe").start(() -> {
            try {
                // 任何 HTTP 响应都证明可达（404/405 也算）
                HttpRequest request = HttpRequest.newBuilder(
                                URI.create(trimSlash(props.cimicode().baseUrl()) + "/"))
                        .timeout(Duration.ofSeconds(10))
                        .GET()
                        .build();
                http.send(request, HttpResponse.BodyHandlers.discarding());
                readiness.markCimicodeReady();
                log.info("cimicode reachable: {}", props.cimicode().baseUrl());
            } catch (Exception e) {
                readiness.markCimicodeFailed(e.getMessage());
                log.warn("cimicode unreachable: {}", e.getMessage());
            }
        });
    }

    /**
     * 发起 context_prompt 并阻塞消费 SSE 流直到 done 或中断。
     *
     * @return true = 正常 done；false = onInterrupted 已触发（流中断，未 done）
     */
    public boolean stream(ContextPromptModels.ContextPromptRequest request, StreamListener listener)
            throws Exception {
        String url = trimSlash(props.cimicode().baseUrl()) + "/session/context_prompt";
        HttpRequest httpRequest = HttpRequest.newBuilder(URI.create(url))
                // 不设总 timeout（SSE 长流），用读循环内的 deadline 控制
                .header("Content-Type", "application/json")
                .header("Accept", "text/event-stream")
                .POST(HttpRequest.BodyPublishers.ofString(
                        mapper.writeValueAsString(request), StandardCharsets.UTF_8))
                .build();

        HttpResponse<java.io.InputStream> response =
                http.send(httpRequest, HttpResponse.BodyHandlers.ofInputStream());
        if (response.statusCode() / 100 != 2) {
            String body = new String(response.body().readAllBytes(), StandardCharsets.UTF_8);
            throw new IOException("context_prompt HTTP " + response.statusCode() + ": " + body);
        }

        Instant deadline = Instant.now().plusMillis(props.cimicode().requestTimeoutMs());
        try (BufferedReader reader = new BufferedReader(
                new InputStreamReader(response.body(), StandardCharsets.UTF_8))) {
            var frames = SseParser.parse(reader);
            while (frames.hasNext()) {
                if (Instant.now().isAfter(deadline)) {
                    throw new IOException("SSE stream exceeded "
                            + props.cimicode().requestTimeoutMs() + "ms without done");
                }
                SseParser.Frame frame = frames.next();
                JsonNode data = parseJson(frame.data());
                String type = frame.event() != null
                        ? frame.event()
                        : data == null ? null : data.path("type").asText(null);

                if ("done".equals(type)) {
                    listener.onDone();
                    return true;
                }
                if (data != null || frame.event() != null) {
                    listener.onEvent(type, data);
                }
            }
            // 流自然结束但没有 done：视为中断（R1）
            listener.onInterrupted(new IOException("SSE stream ended without done event"));
            return false;
        } catch (IOException e) {
            listener.onInterrupted(e);
            return false;
        }
    }

    private JsonNode parseJson(String data) {
        if (data == null || data.isBlank()) {
            return null;
        }
        try {
            return mapper.readTree(data);
        } catch (Exception e) {
            log.debug("non-JSON SSE data line ignored: {}", data);
            return null;
        }
    }

    private static String trimSlash(String url) {
        return url.endsWith("/") ? url.substring(0, url.length() - 1) : url;
    }
}
