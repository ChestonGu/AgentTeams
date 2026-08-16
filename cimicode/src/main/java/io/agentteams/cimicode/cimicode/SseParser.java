package io.agentteams.cimicode.cimicode;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.UncheckedIOException;
import java.util.ArrayList;
import java.util.Iterator;
import java.util.List;
import java.util.NoSuchElementException;

/**
 * SSE 帧解析器（W3C event-stream 最小实现：event:/data: 行 + 空行分帧，
 * 忽略注释行与 retry:）。data 多行按 \n 拼接。
 */
public final class SseParser {

    public record Frame(String event, String data) {
    }

    private SseParser() {
    }

    /** 惰性迭代：每读到一个完整帧产出一次。 */
    public static Iterator<Frame> parse(BufferedReader reader) {
        return new Iterator<>() {
            private Frame next;

            @Override
            public boolean hasNext() {
                if (next != null) {
                    return true;
                }
                try {
                    next = readFrame(reader);
                    return next != null;
                } catch (IOException e) {
                    throw new UncheckedIOException(e);
                }
            }

            @Override
            public Frame next() {
                if (!hasNext()) {
                    throw new NoSuchElementException();
                }
                Frame frame = next;
                next = null;
                return frame;
            }
        };
    }

    /** 便于测试：一次性解析全部帧。 */
    public static List<Frame> parseAll(BufferedReader reader) throws IOException {
        List<Frame> frames = new ArrayList<>();
        Iterator<Frame> it = parse(reader);
        while (it.hasNext()) {
            frames.add(it.next());
        }
        return frames;
    }

    private static Frame readFrame(BufferedReader reader) throws IOException {
        String event = null;
        StringBuilder data = null;
        boolean sawAny = false;
        String line;
        while ((line = reader.readLine()) != null) {
            if (line.isEmpty()) {
                if (sawAny) {
                    return new Frame(event, data == null ? null : data.toString());
                }
                continue;
            }
            if (line.startsWith(":")) {
                continue; // 注释/心跳
            }
            sawAny = true;
            int colon = line.indexOf(':');
            String field = colon < 0 ? line : line.substring(0, colon);
            String value = colon < 0 ? "" : line.substring(colon + 1);
            if (value.startsWith(" ")) {
                value = value.substring(1);
            }
            switch (field) {
                case "event" -> event = value;
                case "data" -> {
                    if (data == null) {
                        data = new StringBuilder();
                    } else {
                        data.append('\n');
                    }
                    data.append(value);
                }
                default -> { /* retry:/id: 忽略 */ }
            }
        }
        // EOF 时补发最后一帧（无尾随空行的容错）
        if (sawAny) {
            return new Frame(event, data == null ? null : data.toString());
        }
        return null;
    }
}
