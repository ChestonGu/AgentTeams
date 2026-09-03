from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FilterDecision:
    accepted: bool
    reason: str
    role: str
    mentions: list[str]


@dataclass
class RoleResolver:
    self_user_id: str | None = None
    leader: str | None = None
    admin: str | None = None
    workers: set[str] = field(default_factory=set)

    def role_for(self, sender: str | None) -> str:
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
        if not self.leader and not self.admin and not self.workers:
            return "human"
        return "unknown"


@dataclass
class MentionFilter:
    aliases: set[str] = field(default_factory=lambda: {"leader", "manager", "team"})
    user_id: str | None = None
    require_mention: bool = True
    allow_unknown: bool = False
    allowed_roles: set[str] = field(default_factory=lambda: {"leader", "admin", "human"})
    role_resolver: RoleResolver = field(default_factory=RoleResolver)

    @staticmethod
    def _normalize_user_id(value: str | None) -> str:
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

    _normalize_mention = _normalize_user_id

    @staticmethod
    def _extract_mentions_from_matrix_content(content: dict[str, Any] | None) -> list[str]:
        if not isinstance(content, dict):
            return []

        mentions: list[str] = []
        raw_mentions = content.get("m.mentions", {}) if isinstance(content.get("m.mentions", {}), dict) else {}
        user_ids = raw_mentions.get("user_ids", []) if isinstance(raw_mentions, dict) else []
        if isinstance(user_ids, list):
            mentions.extend(str(item) for item in user_ids)

        formatted_body = content.get("formatted_body")
        if isinstance(formatted_body, str):
            for match in re.findall(r"https?://matrix\.to/#/([^\"'\s]+)", formatted_body, flags=re.IGNORECASE):
                decoded = urllib.parse.unquote(match)
                if decoded.startswith("@"):
                    mentions.append(decoded)

        return list(dict.fromkeys(mentions))

    @staticmethod
    def extract_mentions(text: str) -> list[str]:
        matches = re.findall(r"@([A-Za-z0-9_.=+/-]+(?::[A-Za-z0-9.-]+(?::\d+)?)?)", text)
        converted = []
        for match in matches:
            normalized = MentionFilter._normalize_mention(match)
            if normalized:
                converted.append(normalized)
        return list(dict.fromkeys(converted))

    def mentions_self(
        self,
        event_body: str | None = None,
        *,
        content: dict[str, Any] | None = None,
    ) -> bool:
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
        mentions = self._extract_mentions_from_matrix_content(content)
        if event_body:
            mentions.extend(self.extract_mentions(event_body))
        mentions = list(dict.fromkeys(mentions))

        role = self.role_resolver.role_for(sender)
        if role == "self":
            return FilterDecision(False, "self_message", role, mentions)
        if not is_group and not self.require_mention:
            return FilterDecision(True, "direct_message", role, mentions)
        if self.require_mention and not self.mentions_self(event_body, content=content):
            return FilterDecision(False, "not_mentioned", role, mentions)
        if role == "unknown" and not self.allow_unknown:
            return FilterDecision(False, "unknown_sender", role, mentions)
        if is_group and role not in self.allowed_roles and not (role == "unknown" and self.allow_unknown):
            return FilterDecision(False, "sender_not_allowed", role, mentions)
        return FilterDecision(True, "accepted", role, mentions)

    def should_handle(
        self,
        event_body: str | None = None,
        sender: str | None = None,
        *,
        content: dict[str, Any] | None = None,
    ) -> bool:
        aliases = {self._normalize_mention(alias) for alias in self.aliases}

        normalized_sender = self._normalize_mention(sender)
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
