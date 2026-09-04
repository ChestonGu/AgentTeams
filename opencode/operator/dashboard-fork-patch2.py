"""Patch #2 for the dashboard fork: RUNTIME_META gains the opencode entry.

TS build requires Record<WorkerRuntime, RuntimeMeta> to cover every union
member once 'opencode' joins WorkerRuntime.
"""

import io

P = "/home/extvdiadmin/dashboard-fork/src/lib/runtime-meta.ts"

OLD = """  qwenpaw: {
    icon: Sparkles,
    badgeClass: 'bg-orange-500/10 text-orange-600 dark:text-orange-400 border-orange-500/30',
    description: '完整流式协议，思考以 Thinking: 前缀识别',
  },
};"""

NEW = """  qwenpaw: {
    icon: Sparkles,
    badgeClass: 'bg-orange-500/10 text-orange-600 dark:text-orange-400 border-orange-500/30',
    description: '完整流式协议，思考以 Thinking: 前缀识别',
  },
  opencode: {
    icon: Sparkles,
    badgeClass: 'bg-cyan-500/10 text-cyan-600 dark:text-cyan-400 border-cyan-500/30',
    description: 'cimicode-bridge 伪装 Worker，会话由独立 opencode 服务执行',
  },
};"""

with io.open(P, encoding="utf-8") as f:
    s = f.read()
assert s.count(OLD) == 1, s.count(OLD)
with io.open(P, "w", encoding="utf-8", newline="") as f:
    f.write(s.replace(OLD, NEW))
print("patched runtime-meta.ts")
