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

# team.members[].role 的角色分类（controller member_reconcile.go 的
# MemberRole 投影值）：team_leader → leader；worker → worker；
# 其余（coordinator 等，人类成员）→ humans。admin 单独走 team.admin 段。
TEAM_ROLE_LEADER = "team_leader"
TEAM_ROLE_WORKER = "worker"


@dataclass(frozen=True)
class TeamRoster:
    """runtime.yaml team 段的花名册视图（mention 门禁角色映射的数据源）。

    全部字段已归一化为可空形态：无 team 段的 standalone worker 拿到
    None（见 coordination_roster），而不是全空的 roster——区分"未入队"
    与"入队但某角色缺席"。
    """

    leader: str | None = None         # team_leader 成员的 MXID
    admin: str | None = None          # team.admin.matrixUserId
    workers: frozenset[str] = frozenset()  # role=worker 成员 MXID 集
    humans: frozenset[str] = frozenset()   # 其余成员（coordinator 等）MXID 集


def managed_runtime_type(runtime_yaml: str) -> str:
    """从 MemberRuntimeConfig 快照读取 member.runtime（解析失败返回空串）。

    managed runtime.yaml 是 worker 运行时类型的权威声明（"worker-bridge"）；
    adapter 具体形态（stateless/pod）由顶层 bridge 段的 adapterMode 声明
    （见 WorkerBootstrapConfig.runtime_bridge_section）。
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
    profile_md: str = ""        # PROFILE.md（persona 输入：identity 内容经 inlineConfig 提取）
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
    def runtime_bridge_section(self) -> dict[str, str]:
        """runtime.yaml 顶层 bridge 段（旧平铺形态解析，v1.3 兼容通道）。

        v1.3 之前 controller 把 Worker CR 的绑定四字段平铺投影到 bridge 段
        顶层（camelCase 为 Go 投影形态，snake_case 兼容）；v1.3 起改嵌套
        bridge.runtimeParameter 袋（见 runtime_parameter property）。本
        property 归一化解析旧平铺键（含 adapterMode——分派键不进袋，仍由
        这里透出）；空值字段不出现，归一化为 snake_case 键返回；无段或
        解析失败返回空 dict（bridge 据此保持未定态等待自愈）。
        """
        if not self.runtime_yaml:
            return {}
        try:
            import yaml

            doc = yaml.safe_load(self.runtime_yaml) or {}
            bridge = doc.get("bridge")
            if not isinstance(bridge, dict):
                return {}
            aliases = {
                "adapterMode": "adapter_mode", "adapter_mode": "adapter_mode",
                "baseUrl": "base_url", "base_url": "base_url",
                "sessionId": "session_id", "session_id": "session_id",
                "sandboxId": "sandbox_id", "sandbox_id": "sandbox_id",
                "templateId": "template_id", "template_id": "template_id",
            }
            out: dict[str, str] = {}
            for key, value in bridge.items():
                normalized = aliases.get(str(key))
                if normalized is not None and value:
                    out[normalized] = str(value)
            return out
        except Exception as exc:
            logger.warning("failed to parse bridge section from runtime.yaml: %s", exc)
            return {}

    @property
    def runtime_parameter(self) -> dict[str, str]:
        """runtime.yaml bridge.runtimeParameter 绑定袋（契约 v1.3 嵌套形态）。

        controller 把 Worker CR 的 spec.runtimeParameter 整 map 原样投影到
        bridge.runtimeParameter 子段（camelCase 惯例）；未知键原样保留
        （chat 请求体透传平台）。旧平铺 bridge 段（v1.3 之前的投影形态，
        存量 MinIO runtime.yaml 不会自动重写）没有子段——fallback 从平铺
        段收集已知四键。键保持原样不归一化：未知键一旦改写会破坏透传，
        已知键的 camel/snake 双写法由消费方（app._apply_bridge_section）
        兼容。无段或解析失败返回空 dict。
        """
        if not self.runtime_yaml:
            return {}
        try:
            import yaml

            doc = yaml.safe_load(self.runtime_yaml) or {}
            bridge = doc.get("bridge")
            if not isinstance(bridge, dict):
                return {}
            nested = bridge.get("runtimeParameter")
            if isinstance(nested, dict) and nested:
                out: dict[str, str] = {}
                for key, value in nested.items():
                    if value:
                        out[str(key)] = str(value)
                return out
            # 旧平铺 fallback：已知四键收进袋（平铺段本就只有这四个绑定键）
            flat = self.runtime_bridge_section
            return {
                camel: flat[snake]
                for camel, snake in (
                    ("baseUrl", "base_url"),
                    ("sessionId", "session_id"),
                    ("sandboxId", "sandbox_id"),
                    ("templateId", "template_id"),
                )
                if flat.get(snake)
            }
        except Exception as exc:
            logger.warning("failed to parse runtimeParameter from runtime.yaml: %s", exc)
            return {}

    @property
    def bridge_adapter_mode(self) -> str:
        """bridge 段声明的 adapter 形态（cimicode-stateless / cimicode-pod）。"""
        return self.runtime_bridge_section.get("adapter_mode", "")

    @property
    def member_role(self) -> str:
        """runtime.yaml member.role（team_leader / worker / ...；缺失空串）。

        媒体归档的归档者门控数据源（对齐 copaw _should_archive_media：
        只有 team_leader 归档群聊媒体）。
        """
        if not self.runtime_yaml:
            return ""
        try:
            import yaml

            doc = yaml.safe_load(self.runtime_yaml) or {}
            member = doc.get("member")
            if not isinstance(member, dict):
                return ""
            return str(member.get("role") or "").strip()
        except Exception as exc:
            logger.warning("failed to parse member role from runtime.yaml: %s", exc)
            return ""

    def _team_section(self) -> dict[str, Any]:
        """runtime.yaml team 段（无/解析失败返回空 dict）。"""
        if not self.runtime_yaml:
            return {}
        try:
            import yaml

            doc = yaml.safe_load(self.runtime_yaml) or {}
            team = doc.get("team")
            return team if isinstance(team, dict) else {}
        except Exception as exc:
            logger.warning("failed to parse team section from runtime.yaml: %s", exc)
            return {}

    def is_dm_room(self, room_id: str) -> bool:
        """房间是否本成员的 DM（personalRoomId / leaderDmRoomId 命中）。

        copaw 语义：DM 媒体不进团队归档（群聊才归档）。
        """
        team = self._team_section()
        try:
            import yaml

            doc = yaml.safe_load(self.runtime_yaml) or {}
        except Exception:
            doc = {}
        member = doc.get("member") if isinstance(doc, dict) else None
        dm_ids = {
            str((member or {}).get("personalRoomId") or "").strip(),
            str(team.get("leaderDmRoomId") or "").strip(),
        }
        dm_ids.discard("")
        return str(room_id or "").strip() in dm_ids

    def team_object_key(self, shared_rel_path: str) -> str:
        """把 shared/ 相对路径映射为 bucket 内对象 key。

        storage.sharedPrefix 形如 ``teams/t-l1/shared``：
        ``shared/knowledge/matrix/x`` → ``teams/t-l1/shared/knowledge/matrix/x``
        （与 copaw FileSync 的 shared remote 一致）。缺 sharedPrefix
        （standalone）时路径原样返回（全局 shared/ 前缀）。
        """
        prefix = ""
        try:
            import yaml

            doc = yaml.safe_load(self.runtime_yaml) or {}
            storage = doc.get("storage")
            if isinstance(storage, dict):
                prefix = str(storage.get("sharedPrefix") or "").strip().strip("/")
        except Exception as exc:
            logger.warning("failed to parse storage prefix from runtime.yaml: %s", exc)
            prefix = ""
        if not shared_rel_path.startswith("shared/"):
            return "/".join(part for part in (prefix, shared_rel_path) if part)
        rest = shared_rel_path[len("shared/"):]
        return f"{prefix}/{rest}" if prefix else shared_rel_path

    @property
    def coordination_roster(self) -> TeamRoster | None:
        """从 runtime.yaml team 段提取协作花名册（无 team 段返回 None）。

        controller 建队时把全员花名册（members[].role + matrixUserId，
        leader/admin/worker/coordinator）投影进 team 段——这是 mention
        门禁角色映射的权威数据源（app._refresh_role_resolver 消费）。
        解析失败返回 None（保持未配置语义，触发 RoleResolver 的本地
        human 兜底），不让单条 YAML 瑕疵阻断收侧过滤。
        """
        if not self.runtime_yaml:
            return None
        try:
            import yaml

            doc = yaml.safe_load(self.runtime_yaml) or {}
            team = doc.get("team")
            if not isinstance(team, dict):
                return None
            leader: str | None = None
            workers: set[str] = set()
            humans: set[str] = set()
            members = team.get("members")
            if isinstance(members, list):
                for member in members:
                    if not isinstance(member, dict):
                        continue
                    mxid = str(member.get("matrixUserId") or "").strip()
                    role = str(member.get("role") or "").strip()
                    if not mxid:
                        continue
                    if role == TEAM_ROLE_LEADER and leader is None:
                        leader = mxid
                    elif role == TEAM_ROLE_WORKER:
                        workers.add(mxid)
                    else:
                        humans.add(mxid)
            admin = ""
            admin_section = team.get("admin")
            if isinstance(admin_section, dict):
                admin = str(admin_section.get("matrixUserId") or "").strip()
            # admin 身份优先于 coordinator 重复条目（humanMembers 常含 admin）
            humans.discard(leader or "")
            humans.discard(admin)
            return TeamRoster(
                leader=leader,
                admin=admin or None,
                workers=frozenset(workers),
                humans=frozenset(humans),
            )
        except Exception as exc:
            logger.warning("failed to parse team roster from runtime.yaml: %s", exc)
            return None


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

        两个载体都重试：managed（worker-bridge）worker 可能在 controller 推完
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
        # （matrix token 走 AGENTTEAMS_WORKER_MATRIX_TOKEN env，cimicode 端点
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
            # （managed member 调谐分支写入的 MemberRuntimeConfig 快照）
            runtime_yaml=runtime_yaml,
        )