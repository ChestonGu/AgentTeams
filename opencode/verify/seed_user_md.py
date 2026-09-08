#!/usr/bin/env python3
"""Pre-seed user customization files for a worker before its bridge boots.

Writes agents/<worker>/{AGENTS.md,TOOL.md,IDENTITY.md} into the test MinIO
using the pod's mc + root alias. Run BEFORE applying the r3 team manifests —
the bridge bootstrap loads these exactly once at pod start.
"""
import subprocess
import sys

NS = "opencode-team-test"
WORKER = sys.argv[1] if len(sys.argv) > 1 else "v3-w1"

FILES = {
    "AGENTS.md": """<!-- agentteams-builtin-start -->
# canonical placeholder (user tail below is what the generator merges)
<!-- agentteams-builtin-end -->
# v3-w1 自定义工作规范
- 所有 Python 交付前必须先 `python -m py_compile` 自检再提交
- 每次向协调者汇报（含 TASK_COMPLETED）的最后一行固定输出：CUSTOM-RULES-V3W1-ACTIVE
""",
    "TOOL.md": """# 专属工具约定
- 本任务实现并使用 `tkfmt()` 辅助函数：统一以 `[tk] ` 前缀格式化 CLI 输出行
""",
    "IDENTITY.md": """# 身份补充
- 对外自称：ConfigKit 工程师（v3-w1），汇报中需体现该称呼
""",
}


def mc(*args):
    out = subprocess.run(
        ("kubectl", "-n", NS, "exec", "agentteams-oct-minio-0", "--",
         "/usr/bin/mc", *args),
        capture_output=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.decode()[:200])
    return out.stdout.decode()


for name, body in FILES.items():
    p = subprocess.run(
        ("kubectl", "-n", NS, "exec", "-i", "agentteams-oct-minio-0", "--",
         "/bin/sh", "-c", f"cat > /tmp/{name}"),
        input=body.encode(), capture_output=True, timeout=30)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode()[:200])
    mc("cp", f"/tmp/{name}",
       f"root/agentteams-storage/agents/{WORKER}/{name}")
    print(f"seeded agents/{WORKER}/{name} ({len(body)} bytes)")
print("done")
