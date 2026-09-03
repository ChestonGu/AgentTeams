from __future__ import annotations

import html
import re
from urllib.parse import quote


def build_agent_md(
    *,
    agents_md: str = "",
    soul_md: str = "",
    role: str = "worker",
    leader: str = "",
    team: str = "",
    room: str = "",
    admin: str = "",
    workers: str = "",
) -> str:
    coordination = "\n".join([
        "## Coordination",
        f"- Role: {role}",
        f"- Leader: {leader or 'unknown'}",
        f"- Team: {team or 'unknown'}",
        f"- Room: {room or 'unknown'}",
        f"- Admin: {admin or 'unknown'}",
        f"- Workers: {workers or 'unknown'}",
        "- Respond only to the latest message that explicitly mentions you.",
    ])
    parts = [coordination]
    if agents_md:
        parts.append("## AGENTS.md\n" + agents_md)
    if soul_md:
        parts.append("## SOUL.md\n" + soul_md)
    return "\n\n".join(parts)


def render_matrix_message(body: str, *, mention_user_ids: list[str] | None = None) -> dict[str, object]:
    mentions = list(dict.fromkeys(mention_user_ids or _extract_user_ids(body)))
    formatted = html.escape(body).replace("\n", "<br>\n")
    for user_id in mentions:
        anchor = f'<a href="https://matrix.to/#/{quote(user_id, safe="")}">{html.escape(user_id)}</a>'
        formatted = formatted.replace(html.escape(user_id), anchor, 1)
    message: dict[str, object] = {"msgtype": "m.text", "body": body}
    if mentions:
        message.update({
            "format": "org.matrix.custom.html",
            "formatted_body": formatted,
            "m.mentions": {"user_ids": mentions},
        })
    return message


def _extract_user_ids(body: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"@[A-Za-z0-9._=+/-]+:[A-Za-z0-9.-]+(?::\d+)?", body)))
