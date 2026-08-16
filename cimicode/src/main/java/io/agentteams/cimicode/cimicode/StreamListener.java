package io.agentteams.cimicode.cimicode;

import com.fasterxml.jackson.databind.JsonNode;

/**
 * SSE 事件回调。onEvent 在 IO 线程上调用，实现方应快速返回或自行转线程。
 */
public interface StreamListener {

    /**
     * @param type 事件类型（message.part.delta / session.status / session.error / ...）
     * @param data 事件 JSON payload（无 JSON 的行 data 为 null）
     */
    void onEvent(String type, JsonNode data);

    /** 收到 done 终态。 */
    void onDone();

    /** 流异常中断（未收到 done）。 */
    void onInterrupted(Exception cause);
}
