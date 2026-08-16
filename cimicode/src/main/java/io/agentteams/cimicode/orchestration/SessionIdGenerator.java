package io.agentteams.cimicode.orchestration;

import java.security.SecureRandom;
import java.time.Instant;

/**
 * sessionID 生成器，复刻 cimicode 后端格式（规格 ③）：
 * ses_&lt;12 位 hex 降序时间戳&gt;&lt;14 位随机 base62&gt;，需过 cimicode 侧 Zod 校验。
 *
 * 降序时间戳保证字典序与时间序相反（越新越小），与 cimicode Identifier.create() 一致。
 */
public final class SessionIdGenerator {

    private static final char[] BASE62 =
            "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz".toCharArray();
    private static final SecureRandom RANDOM = new SecureRandom();
    private static final long HEX_SPACE = 0xFFFF_FFFF_FFFFL; // 12 hex 位

    private SessionIdGenerator() {
    }

    public String next() {
        long epochSeconds = Instant.now().getEpochSecond();
        long descending = HEX_SPACE - epochSeconds;
        String prefix = String.format("%012x", descending);
        StringBuilder suffix = new StringBuilder(14);
        for (int i = 0; i < 14; i++) {
            suffix.append(BASE62[RANDOM.nextInt(BASE62.length)]);
        }
        return "ses_" + prefix + suffix;
    }

    /** 消息 ID（本地历史导出用，cimicode 不校验其格式）。 */
    public String nextMessageId() {
        StringBuilder sb = new StringBuilder("msg_");
        for (int i = 0; i < 16; i++) {
            sb.append(BASE62[RANDOM.nextInt(BASE62.length)]);
        }
        return sb.toString();
    }
}
