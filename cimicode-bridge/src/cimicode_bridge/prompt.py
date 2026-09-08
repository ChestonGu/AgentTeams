"""v2.4 agent.md generator 的子进程封装（契约 §6）。

generator 随 bridge 镜像分发（与源模板成对，契约 §2），对非法输入 fail-loud
（exit 1）——bridge 侧同样镜像该行为：任何非零退出都抛 GenerateAgentMdError，
调用方（app）拒绝对该 turn 放行。
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# generator 在 bridge 镜像内的默认路径
DEFAULT_GENERATOR_PATH = "/opt/agenttools/generate_agent_md.py"


class GenerateAgentMdError(RuntimeError):
    """v2.4 generator 执行失败——bridge 必须拒绝开启本轮会话。"""


def build_agent_md_via_generator(
    *,
    runtime_yaml: str,
    soul_md: str = "",
    profile_md: str = "",
    generator_path: str | None = None,
    template_path: str | None = None,
) -> str:
    """调用 v2.4 generator 渲染 agent.md（契约 §6）。

    runtime_yaml 为空时 fail-loud；generator 不存在同样 fail-loud。
    soul/profile 作为可选输入（--soul-file / --profile-file）传入。
    """
    generator = generator_path or os.getenv("BRIDGE_GENERATOR_PATH", DEFAULT_GENERATOR_PATH)
    if not Path(generator).exists():
        raise GenerateAgentMdError(f"generator not found: {generator}")
    if not runtime_yaml.strip():
        raise GenerateAgentMdError(
            "runtime.yaml is empty/missing — cannot render agent.md (fail-loud, refusing turn)"
        )

    with tempfile.TemporaryDirectory(prefix="bridge-agent-md-") as tmp:
        runtime_path = Path(tmp) / "runtime.yaml"
        runtime_path.write_text(runtime_yaml, encoding="utf-8")
        command = [
            sys.executable,  # 与 bridge 同一个解释器（连带其 PyYAML）
            generator,
            "--runtime-config",
            str(runtime_path),
            "--output",
            "-",
        ]
        if template_path or os.getenv("BRIDGE_GENERATOR_TEMPLATE"):
            command += ["--template", template_path or os.getenv("BRIDGE_GENERATOR_TEMPLATE", "")]
        if soul_md.strip():
            soul_path = Path(tmp) / "SOUL.md"
            soul_path.write_text(soul_md, encoding="utf-8")
            command += ["--soul-file", str(soul_path)]
        if profile_md.strip():
            profile_path = Path(tmp) / "PROFILE.md"
            profile_path.write_text(profile_md, encoding="utf-8")
            command += ["--profile-file", str(profile_path)]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            # generator 输出 UTF-8（§-标记、JSONL 日志）——绝不让平台默认
            # 编码（Windows 主机的 GBK）毁掉它
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise GenerateAgentMdError(
            f"generate_agent_md.py exited {result.returncode}: {stderr[:800]}"
        )
    agent_md = result.stdout
    if not agent_md.strip():
        raise GenerateAgentMdError("generate_agent_md.py produced empty output")
    logger.info("agent.md generated (%d bytes) via %s", len(agent_md), generator)
    return agent_md
