"""Matrix 消息的 mention 检测与角色过滤（收侧硬过滤链）。

对齐 CoPaw 语义：m.mentions 结构化字段 / matrix.to 链接 / 文本正则三级检测，
叠加角色白名单（leader/admin/human 放行、worker 互 @ 阻断、unknown 拒绝）。
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FilterDecision:
    """一次消息的过滤判定结果。"""

    accepted: bool      # 是否放行进入 turn 处理
    reason: str         # 拒绝原因（self_message / not_mentioned / unknown_sender / sender_not_allowed）
    role: str           # 发送者角色（self/leader/admin/worker/human/unknown）
    mentions: list[str]  # 消息中检测到的全部 mention


@dataclass
class RoleResolver:
    """依据 COORDINATION_* 配置把 Matrix 用户 ID 解析为协作角色。"""

    self_user_id: str | None = None   # 自身 MXID → "self"
    leader: str | None = None         # Leader MXID → "leader"
    admin: str | None = None          # 管理员 MXID → "admin"
    workers: set[str] = field(default_factory=set)  # 其他 worker MXID 集合 → "worker"

    def role_for(self, sender: str | None) -> str:
        """返回发送者的角色；无任何映射配置时视为 human（本地开发兼容）。"""
        normalized = MentionFilter._normalize_user_id(sender)
        if not normalized:
            return "unknown"
        if self.self_user_id and normalized == MentionFilter._normalize_user_id(self.self_user_id):
            return "self"
        if self.leader and normalized == MentionFilter._normalize_user_id(self.leader):
            return "leader"
        if self.admin and normalized == MentionFilter._normalize_user_id(self.admin):
            return "admin"
        if normalized in {MentionFilter._normalize_user_id(item) for item in self.workers}:
            return "worker"
        # 未配置任何角色映射的本地模式：把 sender 当人类用户，方便联调
        if not self.leader and not self.admin and not self.workers:
            return "human"
        return "unknown"


@dataclass
class MentionFilter:
    """mention 硬过滤器：require_mention → 角色白名单 逐级判定。"""

    aliases: set[str] = field(default_factory=lambda: {"leader", "manager", "team"})  # 本地开发别名
    user_id: str | None = None                 # 自身 MXID（whoami 后回填）
    require_mention: bool = True               # 群聊是否必须 @ 自己才触发
    allow_unknown: bool = False                # 是否放行未知发送者
    allowed_roles: set[str] = field(default_factory=lambda: {"leader", "admin", "human"})  # 白名单角色
    role_resolver: RoleResolver = field(default_factory=RoleResolver)  # 角色解析器

    @staticmethod
    def _normalize_user_id(value: str | None) -> str:
        """归一化 MXID：剥 <>、去 @、URL 解码、取 localpart、转小写。"""
        if not value:
            return ""
        cleaned = value.strip()
        cleaned = cleaned.strip("<>")
        if cleaned.startswith("@"):
            cleaned = cleaned[1:]
        cleaned = urllib.parse.unquote(cleaned)
        if ":" in cleaned:
            cleaned = cleaned.split(":", 1)[0]
        return cleaned.lower()

    _normalize_mention = _normalize_user_id  # 历史别名

    @staticmethod
    def _extract_mentions_from_matrix_content(content: dict[str, Any] | None) -> list[str]:
        """从 Matrix 事件 content 提取结构化 mention：m.mentions.user_ids + formatted_body 链接。"""
        if not isinstance(content, dict):
            return []

        mentions: list[str] = []
        # 来源 1：Matrix 规范的结构化 mention 字段
        raw_mentions = content.get("m.mentions", {}) if isinstance(content.get("m.mentions", {}), dict) else {}
        user_ids = raw_mentions.get("user_ids", []) if isinstance(raw_mentions, dict) else []
        if isinstance(user_ids, list):
            mentions.extend(str(item) for item in user_ids)

        # 来源 2：formatted_body 里的 matrix.to 锚点链接（Element 发出的形式）
        formatted_body = content.get("formatted_body")
        if isinstance(formatted_body, str):
            for match in re.findall(r"https?://matrix\.to/#/([^\"'\s]+)", formatted_body, flags=re.IGNORECASE):
                decoded = urllib.parse.unquote(match)
                if decoded.startswith("@"):
                    mentions.append(decoded)

        return list(dict.fromkeys(mentions))

    @staticmethod
    def _extract_user_ids(body: str) -> list[str]:
        """从纯文本 body 正则提取 MXID。"""
        return list(dict.fromkeys(re.findall(r"@[A-Za-z0-9._=+/-]+:[A-Za-z0-9.-]+(?::\d+)?", body)))

    @staticmethod
    def extract_mentions(text: str) -> list[str]:
        """提取文本中的全部 mention 并归一化去重（静态方法）。"""
        matches = re.findall(r"@([A-Za-z0-9_.=+/-]+(?::[A-Za-z0-9.-]+(?::\d+)?)?)", text)
        converted = []
        for match in matches:
            normalized = MentionFilter._normalize_user_id(match)
            if normalized:
                converted.append(normalized)
        return list(dict.fromkeys(converted))

    def mentions_self(
        self,
        event_body: str | None = None,
        *,
        content: dict[str, Any] | None = None,
    ) -> bool:
        """判断消息是否 @ 了自己（配置了 user_id 时精确匹配，否则回退别名）。"""
        mentions = self._extract_mentions_from_matrix_content(content)
        if event_body:
            mentions.extend(self.extract_mentions(event_body))
        if self.user_id:
            target = self._normalize_user_id(self.user_id)
            return any(self._normalize_user_id(item) == target for item in mentions)
        aliases = {self._normalize_user_id(alias) for alias in self.aliases}
        return any(self._normalize_user_id(item) in aliases for item in mentions)

    def evaluate(
        self,
        event_body: str | None = None,
        sender: str | None = None,
        *,
        content: dict[str, Any] | None = None,
        is_group: bool = True,
    ) -> FilterDecision:
        """完整过滤链：自发消息 → DM 直通 → require_mention → 角色白名单。"""
        # 汇总该消息的全部 mention（结构化 + 文本）
        mentions = self._extract_mentions_from_matrix_content(content)
        if event_body:
            mentions.extend(self.extract_mentions(event_body))
        mentions = list(dict.fromkeys(mentions))

        role = self.role_resolver.role_for(sender)
        if role == "self":  # 自己发的消息直接忽略
            return FilterDecision(False, "self_message", role, mentions)
        if not is_group and not self.require_mention:  # DM 且不要求 mention → 直通
            return FilterDecision(True, "direct_message", role, mentions)
        if self.require_mention and not self.mentions_self(event_body, content=content):
            return FilterDecision(False, "not_mentioned", role, mentions)  # 未 @ 自己 → 进 history
        if role == "unknown" and not self.allow_unknown:
            return FilterDecision(False, "unknown_sender", role, mentions)  # 未知发送者默认拒绝
        if is_group and role not in self.allowed_roles and not (role == "unknown" and self.allow_unknown):
            return FilterDecision(False, "sender_not_allowed", role, mentions)  # 角色不在白名单（如 worker 互 @）
        return FilterDecision(True, "accepted", role, mentions)

    def should_handle(
        self,
        event_body: str | None = None,
        sender: str | None = None,
        *,
        content: dict[str, Any] | None = None,
    ) -> bool:
        """简化版判定（仅返回布尔），供调试/测试使用。"""
        aliases = {self._normalize_user_id(alias) for alias in self.aliases}

        normalized_sender = self._normalize_user_id(sender)
        if normalized_sender and normalized_sender in aliases:
            return True

        mentions: list[str] = []
        if content is not None:
            mentions.extend(self._extract_mentions_from_matrix_content(content))
        if event_body:
            mentions.extend(self.extract_mentions(event_body))

        if not mentions:
            return False

        if self.user_id:
            return any(self._normalize_user_id(m) == self._normalize_user_id(self.user_id) for m in mentions)
        return any(self._normalize_mention(m) in aliases for m in mentions)
