"""S3/MinIO 启动引导：拉取调谐写入的 worker 配置（openclaw.json / runtime.yaml 双载体）。"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

from minio import Minio

logger = logging.getLogger(__name__)


def managed_runtime_type(runtime_yaml: str) -> str:
    """从 MemberRuntimeConfig 快照读取 member.runtime（解析失败返回空串）。

    managed runtime.yaml 是 worker 运行时类型的权威声明——
    自愈轮询用它自裁决 opencode adapter（不等 operator 的 BRIDGE_RUNTIME_* env）。
    """
    try:
        import yaml

        doc = yaml.safe_load(runtime_yaml) or {}
        member = doc.get("member") or {}
        return str(member.get("runtime") or "")
    except Exception:
        return ""


def inline_persona(runtime_yaml: str) -> tuple[str, str]:
    """从 MemberRuntimeConfig 快照提取 (soul, identity-as-profile)。

    Worker spec.soul / spec.identity 被 DeployMemberRuntimeConfig 投影进
    desired.inlineConfig；YAML 解析失败降级为空串（generator 视两者均为可选），
    不会让整个 bootstrap 失败。
    """
    try:
        import yaml

        doc = yaml.safe_load(runtime_yaml) or {}
        inline = ((doc.get("desired") or {}).get("inlineConfig")) or {}
        if not isinstance(inline, dict):
            return "", ""
        return str(inline.get("soul") or ""), str(inline.get("identity") or "")
    except Exception as exc:
        logger.warning("failed to parse inlineConfig from runtime.yaml: %s", exc)
        return "", ""


@dataclass
class WorkerBootstrapConfig:
    """S3 拉取结果（仅存内存，不落盘）。

    openclaw.json 解析为 dict；AGENTS.md / SOUL.md / PROFILE.md / runtime.yaml
    保持原文。兼容 camelCase/snake_case 两种字段写法。
    """

    openclaw: dict[str, Any]
    agents_md: str = ""
    soul_md: str = ""
    profile_md: str = ""        # PROFILE.md（opencode persona 输入）
    runtime_yaml: str = ""      # agents/<name>/runtime/runtime.yaml（MemberRuntimeConfig 快照）

    @property
    def matrix_config(self) -> dict[str, Any]:
        """取 channels.matrix 配置段（无则空 dict）。"""
        channels = self.openclaw.get("channels", {})
        return channels.get("matrix", {}) if isinstance(channels, dict) else {}

    @property
    def matrix_access_token(self) -> str:
        """Matrix access token（accessToken / access_token 双写法兼容）。"""
        matrix = self.matrix_config
        return str(matrix.get("accessToken") or matrix.get("access_token") or "")

    @property
    def bridge_runtime_config(self) -> dict[str, Any]:
        """取 bridge.runtime 配置段（gateway 绑定信息）。"""
        bridge = self.openclaw.get("bridge", {})
        if not isinstance(bridge, dict):
            return {}
        runtime = bridge.get("runtime", {})
        return runtime if isinstance(runtime, dict) else {}

    @property
    def gateway_session_id(self) -> str:
        """gateway 预创建的 sessionId。"""
        return str(
            self.bridge_runtime_config.get("sessionId")
            or self.bridge_runtime_config.get("session_id")
            or ""
        )

    @property
    def gateway_sandbox_id(self) -> str:
        """gateway 预创建的 sandboxId。"""
        return str(
            self.bridge_runtime_config.get("sandboxId")
            or self.bridge_runtime_config.get("sandbox_id")
            or ""
        )

    @property
    def runtime_helper_url(self) -> str:
        """sandbox AGENTS.md helper 服务地址（opencode adapter 用）。"""
        return str(
            self.bridge_runtime_config.get("helperUrl")
            or self.bridge_runtime_config.get("helper_url")
            or ""
        )


class S3Bootstrap:
    """MinIO S3 客户端封装：按 env 装配，按固定 key 拉取 worker 配置。"""

    def __init__(self, *, client: Minio, bucket: str, prefix: str = "") -> None:
        self.client = client
        self.bucket = bucket
        self.prefix = prefix.strip("/")

    @classmethod
    def from_environment(cls) -> "S3Bootstrap | None":
        """工厂方法：从 AGENTTEAMS_FS_* env 装配客户端；四项不全返回 None（本地模式）。"""
        endpoint = os.getenv("AGENTTEAMS_FS_ENDPOINT", "")
        access_key = os.getenv("AGENTTEAMS_FS_ACCESS_KEY", "")
        secret_key = os.getenv("AGENTTEAMS_FS_SECRET_KEY", "")
        bucket = os.getenv("AGENTTEAMS_FS_BUCKET", "")
        if not endpoint or not access_key or not secret_key or not bucket:
            return None

        endpoint = endpoint.removeprefix("http://").removeprefix("https://")
        secure = os.getenv("AGENTTEAMS_FS_SECURE", "").lower() in {"1", "true", "yes"}
        # 注意：AGENTTEAMS_STORAGE_PREFIX 是 copaw 运行时使用的 mc 别名/bucket 形态
        # （如 "agentteams/agentteams-storage"）——不是 S3 key 前缀。controller 写入的
        # bootstrap 对象就在 AGENTTEAMS_FS_BUCKET 内的 agents/<name>/... 路径下。
        return cls(
            client=Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure),
            bucket=bucket,
            prefix="",
        )

    def _key(self, name: str) -> str:
        """拼对象 key：{STORAGE_PREFIX}/agents/{WORKER_NAME}/{name}。"""
        worker_name = os.getenv("AGENTTEAMS_WORKER_NAME", "")
        parts = [self.prefix, "agents", worker_name, name]
        return "/".join(part.strip("/") for part in parts if part.strip("/"))

    def read_text(self, name: str) -> str | None:
        """读单个对象全文（UTF-8）；失败仅 warning 并返回 None。"""
        try:
            response = self.client.get_object(self.bucket, self._key(name))
            try:
                return response.read().decode("utf-8")
            finally:
                response.close()
                response.release_conn()
        except Exception as exc:
            logger.warning("failed to read bootstrap object %s: %s", name, exc)
            return None

    def publish(self, name: str, text: str) -> str | None:
        """把 UTF-8 文本写到 agents/<worker>/<name> 下；返回写入的 key。

        尽力而为的观测通道（如生成出来的 agent.md）：失败仅告警并返回
        None——绝不影响 turn 主流程。
        """
        key = self._key(name)
        try:
            from io import BytesIO

            self.client.put_object(
                self.bucket, key, BytesIO(text.encode("utf-8")), len(text.encode("utf-8")),
                content_type="text/markdown",
            )
            return key
        except Exception as exc:
            logger.warning("failed to publish object %s: %s", key, exc)
            return None

    def load(self, *, retries: int = 6, retry_interval_seconds: float = 5) -> WorkerBootstrapConfig | None:
        """加载双载体：openclaw.json + runtime/runtime.yaml（各自独立重试）。

        两个载体都重试：managed（opencode）worker 可能在 controller 推完
        runtime/runtime.yaml 之前启动，单次不重试的读取曾把 bootstrap 卡在
        不完整状态。openclaw.json 是 legacy cimicode 路径的载体
        （bridge.runtime session/sandbox 绑定）；managed 运行时（qwenpaw
        投影路径）不写 openclaw.json，仅 runtime.yaml 即为完整 bootstrap。
        """
        openclaw_text = None
        runtime_yaml = ""
        for attempt in range(retries):
            openclaw_text = openclaw_text or self.read_text("openclaw.json")
            runtime_yaml = runtime_yaml or self.read_text("runtime/runtime.yaml")
            if openclaw_text or runtime_yaml:
                break
            if attempt + 1 < retries:
                time.sleep(retry_interval_seconds)
        # runtime.yaml-only 引导：persona 从 MemberRuntimeConfig 快照提取
        # （matrix token 走 AGENTTEAMS_WORKER_MATRIX_TOKEN env，opencode 端点
        # 走 BRIDGE_RUNTIME_* env）。
        if not openclaw_text:
            if not runtime_yaml:
                return None
            soul_md, profile_md = inline_persona(runtime_yaml)
            return WorkerBootstrapConfig(
                openclaw={},
                runtime_yaml=runtime_yaml,
                soul_md=soul_md,
                profile_md=profile_md,
            )
        try:
            openclaw = json.loads(openclaw_text)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid S3 openclaw.json") from exc
        return WorkerBootstrapConfig(
            openclaw=openclaw,
            agents_md=self.read_text("AGENTS.md") or "",
            soul_md=self.read_text("SOUL.md") or "",
            profile_md=self.read_text("PROFILE.md") or "",
            # v2.4 generator 输入：agents/<name>/runtime/runtime.yaml
            # （qwenpaw/opencode member 调谐分支写入的 MemberRuntimeConfig 快照）
            runtime_yaml=runtime_yaml,
        )