from __future__ import annotations

import html
import logging
import re
from urllib.parse import quote

logger = logging.getLogger(__name__)


def _md_to_html(text: str) -> str:
    """Convert Markdown to Matrix ``formatted_body``, matching CoPaw/OpenClaw."""
    try:
        from markdown_it import MarkdownIt

        md = MarkdownIt(
            "commonmark",
            {
                "html": False,
                "linkify": True,
                "breaks": True,
                "typographer": False,
            },
        )
        md.enable("strikethrough")
        md.enable("table")

        try:
            from linkify_it import LinkifyIt

            md.linkify = LinkifyIt()
        except ImportError:
            logger.debug("linkify-it-py not installed; bare URLs may not be linkified")

        return md.render(text).rstrip("\n")
    except ImportError:
        logger.warning("markdown-it-py not installed; formatted_body will be plain text")
        return html.escape(text).replace("\n", "<br>\n")


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
    formatted = _md_to_html(body)
    for user_id in mentions:
        anchor = f'<a href="https://matrix.to/#/{quote(user_id, safe="")}">{html.escape(user_id)}</a>'
        escaped_mxid = html.escape(user_id)
        if escaped_mxid in formatted:
            formatted = formatted.replace(escaped_mxid, anchor, 1)
        else:
            formatted = f"{anchor} {formatted}" if formatted else anchor
    message: dict[str, object] = {
        "msgtype": "m.text",
        "body": body,
        "format": "org.matrix.custom.html",
        "formatted_body": formatted,
    }
    if mentions:
        message["m.mentions"] = {"user_ids": mentions}
    return message


def _extract_user_ids(body: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"@[A-Za-z0-9._=+/-]+:[A-Za-z0-9.-]+(?::\d+)?", body)))
