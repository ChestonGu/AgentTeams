package io.agentteams.cimicode.orchestration;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.databind.ObjectMapper;
import io.agentteams.cimicode.cimicode.ContextPromptModels;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * 会话编排注册表：roomId -> SessionState 映射 + 会话历史导出。
 *
 * 持久化（规格 §4.2）：
 * - bridge-state.json：映射表（原子写 tmp+move）
 * - sessions/&lt;safeRoomId&gt;.json：每个房间的 ExportData 历史（done 后追加）
 *
 * 桥侧会话键 = Matrix roomId（不依赖 cimicode 的 sessionID 语义，见规格 ③ 分析）。
 */
@Component
public class SessionRegistry {

    private static final Logger log = LoggerFactory.getLogger(SessionRegistry.class);

    private final ObjectMapper mapper;
    private final io.agentteams.cimicode.config.BridgeProperties props;
    private final SessionIdGenerator idGenerator;

    private final Map<String, SessionState> rooms = new ConcurrentHashMap<>();

    public SessionRegistry(ObjectMapper mapper,
                           io.agentteams.cimicode.config.BridgeProperties props,
                           SessionIdGenerator idGenerator) {
        this.mapper = mapper;
        this.props = props;
        this.idGenerator = idGenerator;
        load();
    }

    // ── 映射 ──────────────────────────────────────────────────────────

    public SessionState get(String roomId) {
        return rooms.get(roomId);
    }

    public void put(SessionState state) {
        rooms.put(state.roomId(), state);
        save();
    }

    public java.util.Collection<SessionState> all() {
        return List.copyOf(rooms.values());
    }

    public void remove(String roomId) {
        rooms.remove(roomId);
        save();
    }

    // ── 历史导出 ──────────────────────────────────────────────────────

    /** 读取房间历史；无历史返回 null（首次请求 context.messages 为空数组）。 */
    public ContextPromptModels.ExportData loadHistory(String roomId, String sessionID) {
        Path file = historyFile(roomId);
        if (!Files.isReadable(file)) {
            return null;
        }
        try {
            ContextPromptModels.ExportData data = mapper.readValue(file.toFile(),
                    ContextPromptModels.ExportData.class);
            // 映射可能换过 sandbox -> 新 sessionID，历史仍复用（规格 R4 路径）
            return new ContextPromptModels.ExportData(
                    new ContextPromptModels.SessionInfo(sessionID, data.info() == null
                            ? null : data.info().title()),
                    data.messages() == null ? List.of() : data.messages());
        } catch (IOException e) {
            log.warn("failed to read history {}: {}", file, e.getMessage());
            return null;
        }
    }

    /** done 后追加本轮 user/assistant 消息并落盘（含头部裁剪）。 */
    public void appendTurn(String roomId, String sessionID, String userText, String assistantText) {
        List<ContextPromptModels.MessageEntry> messages = new ArrayList<>();
        ContextPromptModels.ExportData existing = loadHistory(roomId, sessionID);
        if (existing != null && existing.messages() != null) {
            messages.addAll(existing.messages());
        }
        long now = Instant.now().getEpochSecond();
        messages.add(new ContextPromptModels.MessageEntry(
                new ContextPromptModels.MessageInfo(idGenerator.nextMessageId(), now, "user"),
                List.of(ContextPromptModels.Part.text(userText))));
        messages.add(new ContextPromptModels.MessageEntry(
                new ContextPromptModels.MessageInfo(idGenerator.nextMessageId(), now, "assistant"),
                List.of(ContextPromptModels.Part.text(assistantText))));

        int max = Math.max(2, props.session().maxHistoryMessages());
        if (messages.size() > max) {
            messages = new ArrayList<>(messages.subList(messages.size() - max, messages.size()));
        }

        ContextPromptModels.ExportData data = new ContextPromptModels.ExportData(
                new ContextPromptModels.SessionInfo(sessionID, "bridge-" + safe(roomId)),
                messages);
        try {
            Path file = historyFile(roomId);
            Files.createDirectories(file.getParent());
            atomicWrite(file, mapper.writerWithDefaultPrettyPrinter().writeValueAsString(data));
        } catch (IOException e) {
            log.error("failed to persist history for {}: {}", roomId, e.getMessage());
        }
    }

    // ── 持久化 ────────────────────────────────────────────────────────

    @JsonIgnoreProperties(ignoreUnknown = true)
    record StateFile(Map<String, SessionState> rooms) {
    }

    private synchronized void load() {
        Path file = Path.of(props.session().stateFile());
        if (!Files.isReadable(file)) {
            log.info("no bridge state file at {} (fresh start)", file);
            return;
        }
        try {
            StateFile state = mapper.readValue(file.toFile(), StateFile.class);
            if (state.rooms() != null) {
                rooms.putAll(state.rooms());
                log.info("loaded {} room session(s) from {}", rooms.size(), file);
            }
        } catch (IOException e) {
            log.error("failed to load bridge state {}: {} -- starting empty", file, e.getMessage());
        }
    }

    private synchronized void save() {
        try {
            Path file = Path.of(props.session().stateFile());
            Files.createDirectories(file.getParent());
            atomicWrite(file, mapper.writerWithDefaultPrettyPrinter()
                    .writeValueAsString(new StateFile(Map.copyOf(rooms))));
        } catch (IOException e) {
            log.error("failed to save bridge state: {}", e.getMessage());
        }
    }

    private void atomicWrite(Path file, String content) throws IOException {
        Path tmp = file.resolveSibling(file.getFileName() + ".tmp");
        Files.writeString(tmp, content, StandardCharsets.UTF_8);
        Files.move(tmp, file, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE);
    }

    private Path historyFile(String roomId) {
        return Path.of(props.session().historyDir(), safe(roomId) + ".json");
    }

    static String safe(String roomId) {
        return roomId.replaceAll("[^A-Za-z0-9._-]", "_");
    }
}
