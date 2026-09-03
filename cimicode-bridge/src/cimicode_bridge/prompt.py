from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_GENERATOR_PATH = "/opt/agenttools/generate_agent_md.py"


class GenerateAgentMdError(RuntimeError):
    """The v2.4 generator failed — the bridge must refuse to open a session."""


def build_agent_md_via_generator(
    *,
    runtime_yaml: str,
    soul_md: str = "",
    profile_md: str = "",
    generator_path: str | None = None,
    template_path: str | None = None,
) -> str:
    """Render agent.md by invoking the v2.4 generator (contract §6).

    The generator lives in the bridge image (paired with its source template,
    contract §2) and fails loud on invalid input (exit 1) — the bridge mirrors
    that: any non-zero exit raises, the caller refuses the turn.
    """
    generator = generator_path or os.getenv("BRIDGE_GENERATOR_PATH", DEFAULT_GENERATOR_PATH)
    if not Path(generator).exists():
        raise GenerateAgentMdError(f"generator not found: {generator}")
    if not runtime_yaml.strip():
        raise GenerateAgentMdError("runtime.yaml is empty/missing — cannot render agent.md (fail-loud, refusing turn)")

    with tempfile.TemporaryDirectory(prefix="bridge-agent-md-") as tmp:
        runtime_path = Path(tmp) / "runtime.yaml"
        runtime_path.write_text(runtime_yaml, encoding="utf-8")
        command = [
            sys.executable,  # same interpreter (and its PyYAML) as the bridge
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
            # the generator emits UTF-8 (§-markers, JSONL logs) — never let
            # the platform default codec (GBK on Windows hosts) mangle it
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
