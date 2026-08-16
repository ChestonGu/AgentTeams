package io.agentteams.cimicode.readiness;

import org.springframework.stereotype.Component;

import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * 桥的就绪状态聚合：Matrix 首次 sync 成功 + cimicode 可达 + sandbox 可达
 * 三者齐备后 ReadinessReporter 才执行 agt worker report-ready（P3 协议）。
 */
@Component
public class ReadinessState {

    private final AtomicBoolean matrixReady = new AtomicBoolean(false);
    private final AtomicBoolean cimicodeReady = new AtomicBoolean(false);
    private final AtomicBoolean sandboxReady = new AtomicBoolean(false);
    private final Map<String, String> failures = new ConcurrentHashMap<>();

    public void markMatrixReady() {
        matrixReady.set(true);
        failures.remove("matrix");
    }

    public void markMatrixFailed(String reason) {
        failures.put("matrix", reason);
    }

    public void markCimicodeReady() {
        cimicodeReady.set(true);
        failures.remove("cimicode");
    }

    public void markCimicodeFailed(String reason) {
        failures.put("cimicode", reason);
    }

    public void markSandboxReady() {
        sandboxReady.set(true);
        failures.remove("sandbox");
    }

    public void markSandboxFailed(String reason) {
        failures.put("sandbox", reason);
    }

    public boolean allReady() {
        return matrixReady.get() && cimicodeReady.get() && sandboxReady.get();
    }

    public Map<String, String> failures() {
        return Map.copyOf(failures);
    }
}
