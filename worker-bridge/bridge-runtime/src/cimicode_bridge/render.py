"""出站内容渲染：agentMd 拼装 + Markdown→Matrix HTML 消息构造。"""
from __future__ import annotations

import html
import logging
import re
from urllib.parse import quote

logger = logging.getLogger(__name__)


def _md_to_html(text: str) -> str:
    """Markdown → Matrix formatted_body 的安全 HTML（配置对齐 CoPaw/OpenClaw）。

    - html=False：原始 HTML 被转义（防 XSS）
    - linkify=True：裸 URL 自动转链接；breaks=True：单换行转 <br>
    - 启用删除线与表格插件；库缺失时降级为纯转义 + <br>
    """
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
    """每轮 turn 组装发给 gateway 的 agentMd：协作上下文 + AGENTS.md + SOUL.md。"""
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
    """构造出站 Matrix 消息：纯文本 body + formatted_body + 三层 mention。

    mention 目标取显式参数或从 body 正则提取；每个 MXID 首次出现处
    替换为 matrix.to 锚点（Element pill），并写 m.mentions 结构化字段。
    """
    mentions = list(dict.fromkeys(mention_user_ids or _extract_user_ids(body)))
    formatted = _md_to_html(body)
    for user_id in mentions:
        anchor = f'<a href="https://matrix.to/#/{quote(user_id, safe="")}">{html.escape(user_id)}</a>'
        escaped_mxid = html.escape(user_id)
        if escaped_mxid in formatted:
            formatted = formatted.replace(escaped_mxid, anchor, 1)
        else:
            # MXID 不在渲染结果中（如纯 mention 场景）→ 前置补一个锚点
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
    """从文本中提取全部 MXID（去重保序）。"""
    return list(dict.fromkeys(re.findall(r"@[A-Za-z0-9._=+/-]+:[A-Za-z0-9.-]+(?::\d+)?", body)))
