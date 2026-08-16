package io.agentteams.cimicode.config;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Optional;

/**
 * Manager 下发的 openclaw.json 只读加载器。
 *
 * 提取桥需要的两个字段：
 * - LLM provider（网关 baseUrl/apiKey/模型）-> 请求级 model 透传给 cimicode
 * - Matrix homeserver URL（env 缺失时的兜底）
 *
 * 桥不写回 openclaw.json（Matrix token 每次启动重登录获取，P4 语义）。
 */
@Component
public class OpenClawConfigLoader {

    private static final Logger log = LoggerFactory.getLogger(OpenClawConfigLoader.class);

    private final ObjectMapper mapper = new ObjectMapper();

    /** 网关 provider 视图（baseUrl/apiKey/model 可能缺失）。 */
    public record GatewayConfig(String baseUrl, String apiKey, String model) {
    }

    public Optional<GatewayConfig> loadGatewayConfig(String workspace) {
        JsonNode root = readOpenClawJson(workspace);
        if (root == null) {
            return Optional.empty();
        }
        JsonNode providers = root.path("models").path("providers");
        if (!providers.isArray()) {
            return Optional.empty();
        }
        List<JsonNode> candidates = new ArrayList<>();
        providers.forEach(candidates::add);
        // 优先带 baseUrl 的 provider；agentteams 网关的 provider id 通常含 "gateway"。
        return candidates.stream()
                .filter(p -> !p.path("baseUrl").asText("").isBlank())
                .reduce((a, b) -> preferGateway(a, b))
                .map(p -> new GatewayConfig(
                        p.path("baseUrl").asText(null),
                        p.path("apiKey").asText(null),
                        p.path("model").asText(null)));
    }

    public Optional<String> loadMatrixUrl(String workspace) {
        JsonNode root = readOpenClawJson(workspace);
        if (root == null) {
            return Optional.empty();
        }
        String url = root.path("channels").path("matrix").path("url").asText("");
        return url.isBlank() ? Optional.empty() : Optional.of(url);
    }

    private JsonNode readOpenClawJson(String workspace) {
        Path path = Path.of(workspace, "openclaw.json");
        if (!Files.isReadable(path)) {
            log.debug("openclaw.json not found at {}", path);
            return null;
        }
        try {
            return mapper.readTree(Files.readString(path));
        } catch (Exception e) {
            log.warn("failed to parse {}: {}", path, e.getMessage());
            return null;
        }
    }

    private JsonNode preferGateway(JsonNode a, JsonNode b) {
        boolean aGw = a.path("id").asText("").toLowerCase().contains("gateway")
                || a.path("name").asText("").toLowerCase().contains("gateway");
        boolean bGw = b.path("id").asText("").toLowerCase().contains("gateway")
                || b.path("name").asText("").toLowerCase().contains("gateway");
        if (aGw != bGw) {
            return aGw ? a : b;
        }
        return a;
    }

    @JsonIgnoreProperties(ignoreUnknown = true)
    record Provider(String id, String name, String baseUrl, String apiKey, String model) {
    }
}
