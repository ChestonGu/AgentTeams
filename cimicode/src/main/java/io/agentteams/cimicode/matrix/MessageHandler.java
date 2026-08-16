package io.agentteams.cimicode.matrix;

/**
 * Matrix 入站消息回调（MessagePipeline 实现）。
 *
 * 实现方在虚拟线程上被调用；同房间的调用由 MessagePipeline 串行化，
 * 但不同房间可能并发进入。
 */
public interface MessageHandler {

    void handle(String roomId, String senderUserId, String body);
}
