"""
AI News Aggregator - Daily Brief Output

Generates structured daily briefing in README.md and archive files.
"""

import os
import json
import logging
from html import escape
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from edition import EditionError, validate_edition

BEIJING_TZ = timezone(timedelta(hours=8))

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).with_name("templates")
WEEKDAYS_ZH = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")

BRIEF_HEADER = """# 🤖 AI Daily Brief

"""

BRIEF_FOOTER = "[往期简报](./archives/) · [项目说明](./README.md)\n"


def _template(name: str, values: dict[str, str]) -> str:
    content = (TEMPLATE_DIR / name).read_text(encoding="utf-8")
    for key, value in values.items():
        content = content.replace("{{" + key + "}}", value)
    unresolved = [part.split("}}", 1)[0] for part in content.split("{{")[1:] if "}}" in part]
    if unresolved:
        raise EditionError(f"unresolved template fields: {', '.join(unresolved)}")
    return content


def _http_url(value: str, label: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise EditionError(f"{label} must be an absolute http(s) URL")
    return value


def _edition_date(edition_id: str) -> tuple[datetime, str, str]:
    parsed = datetime.strptime(edition_id, "%Y-%m-%d")
    return parsed, f"{parsed.year} 年 {parsed.month} 月 {parsed.day} 日", WEEKDAYS_ZH[parsed.weekday()]


def _source_time(event: dict) -> tuple[str, str]:
    source = next((item for item in event["sources"] if item["kind"] == "primary"), event["sources"][0])
    value = source.get("published_at")
    if not value:
        return "", ""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(BEIJING_TZ)
    if event["window"] == "recent":
        return parsed.date().isoformat(), f"{parsed:%m.%d} 来源日期 · 近期选读"
    return parsed.isoformat(), f"{parsed:%m.%d %H:%M} 北京时间"


RANGE_LABEL = "适用范围"


def _range_items(event: dict) -> list[str]:
    return [item for item in event.get("conditions") or [] if item]


def _verification_note(source: dict) -> str | None:
    note = source.get("note")
    if not note:
        return None
    return f"核验说明（{source['label']}）：{note}"


def _ranges_web_html(event: dict) -> str:
    items = [escape(item) for item in _range_items(event)]
    if not items:
        return ""
    if len(items) == 1:
        return f'<p class="condition"><strong>{RANGE_LABEL}：</strong>{items[0]}</p>'
    body = "".join(f"<li>{item}</li>" for item in items)
    return f'<div class="condition"><strong>{RANGE_LABEL}：</strong><ul>{body}</ul></div>'


def _ranges_email_html(event: dict) -> str:
    items = [escape(item) for item in _range_items(event)]
    if not items:
        return ""
    box = 'style="margin:12px 0;padding:4px 0 4px 12px;border-left:2px solid #c7c2b7;"'
    if len(items) == 1:
        return f'<div {box}><strong>{RANGE_LABEL}：</strong>{items[0]}</div>'
    body = "".join(f"<li>{item}</li>" for item in items)
    return (
        f'<div {box}><strong>{RANGE_LABEL}：</strong>'
        f'<ul style="margin:6px 0 0;padding-left:20px;">{body}</ul></div>'
    )


def _ranges_plain_lines(event: dict) -> list[str]:
    items = _range_items(event)
    if not items:
        return []
    if len(items) == 1:
        return [f"{RANGE_LABEL}：{items[0]}"]
    return [f"{RANGE_LABEL}：", *items]


def _sources_html(event: dict) -> str:
    links = []
    notes = []
    for source in event["sources"]:
        label = escape(source["label"])
        url = escape(_http_url(source["url"], "event source"), quote=True)
        suffix = " · 一手来源" if source["kind"] == "primary" else " · 补充来源"
        if source["verified"]:
            suffix += " · 已核对"
        links.append(f'<a href="{url}" target="_blank" rel="noopener noreferrer">{label}{suffix}</a>')
        note = _verification_note(source)
        if note:
            notes.append(f'<p class="source-note">{escape(note)}</p>')
    return f'<div class="source">{"".join(links)}</div>{"".join(notes)}'


def _event_copy_html(event: dict) -> str:
    facts = "".join(f"<p>{escape(item)}</p>" for item in event["facts"])
    background = ""
    if event["background"]:
        items = "".join(f"<li>{escape(item)}</li>" for item in event["background"])
        background = f"<details><summary>展开：背景与补充</summary><ul>{items}</ul></details>"
    return facts + _ranges_web_html(event) + background + _sources_html(event)


def _event_html(event: dict, index: int, *, lead: bool = False, desk: bool = False) -> str:
    event_dom_id = f"event-{index}"
    title_id = f"{event_dom_id}-title"
    time_value, time_label = _source_time(event)
    time_html = f'<time datetime="{escape(time_value, quote=True)}">{escape(time_label)}</time>' if time_label else ""
    kicker = f'<div class="kicker"><span class="tag">{escape(event["kicker"])}</span>{time_html}</div>'
    title_tag = "h2" if lead else "h3"
    article_class = "lead" if lead else ("desk-story" if desk else "story")
    copy_class = ' class="lead-copy"' if lead else ""
    return (
        f'<article class="{article_class}" id="{event_dom_id}" aria-labelledby="{title_id}">'
        f'{kicker}<{title_tag} id="{title_id}">{escape(event["title"])}</{title_tag}>'
        f'<div{copy_class}>{_event_copy_html(event)}</div></article>'
    )


def _web_sections(events: list[dict]) -> tuple[str, str]:
    lead = next((event for event in events if event["placement"] == "lead"), events[0])
    remaining = [event for event in events if event is not lead]
    stories = [event for event in remaining if event["placement"] != "desk"]
    desk = [event for event in remaining if event["placement"] == "desk"]

    indexed = {id(event): index for index, event in enumerate(events, 1)}
    body = _event_html(lead, indexed[id(lead)], lead=True)
    nav_links = [f'<a href="#event-{indexed[id(lead)]}">今日焦点</a>']
    columns = []
    if stories:
        story_html = "".join(_event_html(event, indexed[id(event)]) for event in stories)
        columns.append(
            '<section class="news-column" id="news" aria-labelledby="news-heading">'
            '<div class="section-heading"><h2 id="news-heading">新闻速览</h2><span lang="en">IN BRIEF</span></div>'
            f"{story_html}</section>"
        )
        nav_links.append('<a href="#news">新闻速览</a>')
    if desk:
        desk_html = "".join(_event_html(event, indexed[id(event)], desk=True) for event in desk)
        columns.append(
            '<aside class="desk-column" id="practice" aria-labelledby="practice-heading">'
            '<div class="section-heading"><h2 id="practice-heading">实操与问答</h2><span lang="en">FIELD NOTES</span></div>'
            f"{desk_html}</aside>"
        )
        nav_links.append('<a href="#practice">实操与问答</a>')
    if columns:
        layout_class = "columns" if len(columns) == 2 else "columns columns--single"
        body += f'<div class="{layout_class}">{"".join(columns)}</div>'
    nav = '<nav class="reading-index" aria-label="本期阅读目录"><span>本期阅读</span>' + "".join(nav_links) + "</nav>"
    return nav, body


def _edition_note(edition: dict) -> str:
    cutoff = datetime.fromisoformat(edition["cutoff_at"].replace("Z", "+00:00")).astimezone(BEIJING_TZ)
    in_window = sum(event["window"] == "in_window" for event in edition["events"])
    recent = sum(event["window"] == "recent" for event in edition["events"])
    note = f"截至 {cutoff.month} 月 {cutoff.day} 日 {cutoff:%H:%M}（北京时间）。本期收录 {in_window} 条窗口内动态"
    if recent:
        note += f"，另附 {recent} 条近期选读"
    failed_sources = sum(source["status"] == "failed" for source in edition["sources"])
    if failed_sources:
        note += f"；另有 {failed_sources} 个来源采集失败，本期覆盖可能不完整"
    return note + "。"


def render_web_edition(document: dict) -> str:
    """Render one validated published edition with the approved responsive layout."""
    edition = validate_edition(document)
    if edition["status"] != "published_candidate":
        raise EditionError("only published_candidate editions can render HTML")
    _, date_label, weekday = _edition_date(edition["edition_id"])
    nav, body = _web_sections(edition["events"])
    cutoff_note = _edition_note(edition)
    return _template(
        "web.html",
        {
            "DESCRIPTION": escape(f"{date_label} AI 日报，含 {len(edition['events'])} 条已策展内容。", quote=True),
            "TITLE": escape(f"AI 日报 · {date_label}"),
            "EDITION_DATE_ISO": edition["edition_id"],
            "EDITION_DATE": date_label,
            "WEEKDAY": weekday,
            "CUTOFF_NOTE": escape(cutoff_note),
            "NAV": nav,
            "BODY": body,
        },
    )


def write_web_edition(editions_dir: Path, document: dict) -> Path:
    edition = validate_edition(document)
    target = Path(editions_dir) / edition["edition_id"] / "index.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_web_edition(edition), encoding="utf-8")
    return target


def _email_event(event: dict, index: int) -> str:
    sources = " · ".join(
        f'<a href="{escape(_http_url(source["url"], "event source"), quote=True)}" style="color:#91472f;">{escape(source["label"])}</a>'
        for source in event["sources"]
    )
    facts = "".join(f'<p style="margin:0 0 10px;">{escape(item)}</p>' for item in event["facts"])
    background = "".join(f"<li>{escape(item)}</li>" for item in event["background"])
    extra = _ranges_email_html(event)
    if background:
        extra += f'<div style="margin:12px 0;color:#65665c;"><strong>背景与补充</strong><ul style="margin:6px 0 0;padding-left:20px;">{background}</ul></div>'
    notes = []
    for source in event["sources"]:
        note = _verification_note(source)
        if note:
            notes.append(
                f'<div style="margin:6px 0 0;font-size:12px;line-height:1.7;color:#65665c;">{escape(note)}</div>'
            )
    notes = "".join(notes)
    border = "" if index == 1 else "border-top:1px solid #c7c2b7;"
    return (
        f'<section style="padding:20px 0;{border}">'
        f'<div style="font-size:12px;color:#91472f;">{escape(event["kicker"])}</div>'
        f"<h2 style=\"margin:5px 0 12px;font-family:'Songti SC',serif;font-size:24px;line-height:1.45;font-weight:700;color:#252621;\">{escape(event['title'])}</h2>"
        f'{facts}{extra}<div style="font-size:13px;line-height:1.7;">{sources}</div>{notes}</section>'
    )


def render_email_html(document: dict, public_url: str | None = None) -> str:
    edition = validate_edition(document)
    if edition["status"] != "published_candidate":
        raise EditionError("only published_candidate editions can render email")
    _, date_label, weekday = _edition_date(edition["edition_id"])
    cutoff_note = _edition_note(edition)
    body = "".join(_email_event(event, index) for index, event in enumerate(edition["events"], 1))
    public_link = ""
    if public_url:
        url = escape(_http_url(public_url, "public_url"), quote=True)
        public_link = f'<a href="{url}" style="color:#91472f;">查看网页版</a>'
    return _template(
        "email.html",
        {
            "TITLE": escape(f"AI 日报 · {date_label}"),
            "EDITION_DATE": date_label,
            "WEEKDAY": weekday,
            "CUTOFF_NOTE": escape(cutoff_note),
            "BODY": body,
            "PUBLIC_LINK": public_link,
        },
    )


def render_email_text(document: dict, public_url: str | None = None) -> str:
    edition = validate_edition(document)
    if edition["status"] != "published_candidate":
        raise EditionError("only published_candidate editions can render email")
    lines = [f"AI 日报｜{edition['edition_id']}", ""]
    for event in edition["events"]:
        lines.extend([event["title"], *event["facts"]])
        lines.extend(_ranges_plain_lines(event))
        lines.append("来源：" + " · ".join(source["url"] for source in event["sources"]))
        for source in event["sources"]:
            note = _verification_note(source)
            if note:
                lines.append(note)
        lines.append("")
    if public_url:
        lines.append("网页版：" + _http_url(public_url, "public_url"))
    return "\n".join(lines).rstrip() + "\n"


def render_group_message(document: dict, public_url: str, max_items: int = 3) -> str:
    edition = validate_edition(document)
    if edition["status"] != "published_candidate":
        raise EditionError("only published_candidate editions can render a group message")
    if type(max_items) is not int or max_items < 1:
        raise EditionError("max_items must be an integer >= 1")
    lines = [f"【AI 日报｜{edition['edition_id']}】"]
    for event in edition["events"][:max_items]:
        lines.append(f"• {event['title']}")
        for item in _ranges_plain_lines(event):
            lines.append(item)
    remaining = len(edition["events"]) - max_items
    if remaining > 0:
        lines.append(f"另有 {remaining} 条，详见网页版。")
    lines.append("网页版：" + _http_url(public_url, "public_url"))
    return "\n".join(lines)


def _source_badge(source: str) -> str:
    """Format source as inline badge."""
    return f"`{source}`"


def _truncate_title(title: str, max_len: int = 120) -> str:
    """Truncate long titles (e.g., GitHub repo descriptions)."""
    # GitHub-style "owner/repo: long description" — keep repo name, truncate desc at 80
    if ": " in title and "/" in title.split(": ")[0]:
        parts = title.split(": ", 1)
        repo_name = parts[0]
        desc = parts[1]
        # Only apply GitHub truncation if prefix looks like a repo (no spaces, short)
        if " " not in repo_name and len(repo_name) < 60:
            repo_max = 80
            avail = max(repo_max - len(repo_name) - 5, 10)
            if len(desc) > avail:
                desc = desc[:avail] + "..."
            return f"{repo_name}: {desc}"
    if len(title) > max_len:
        return title[:max_len - 3] + "..."
    return title


def _display_title(selection: dict, item: dict) -> str:
    """Prefer the editor's Chinese short title for selected items."""
    title_zh = selection.get("title_zh")
    if isinstance(title_zh, str) and title_zh.strip():
        return title_zh.strip()
    return _truncate_title(item["title"])


def _format_related(item: dict) -> str:
    """Format related sources as inline links."""
    related = item.get("related_sources", [])
    if not related:
        return ""
    links = " · ".join(f"[{r['source']}]({r['url']})" for r in related[:3])
    return f"  📎 延伸: {links}\n"


def _importance_star(item: dict, threshold: int = 9) -> str:
    """Show ⭐ only for high-importance items."""
    return " ⭐" if item.get("importance", 0) >= threshold else ""


def format_daily_brief(curation_result: dict, config: dict) -> str:
    """Generate daily brief README content."""
    candidates = curation_result["candidates"]
    brief = curation_result["brief"]
    date = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
    weekday_zh = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    weekday = weekday_zh[datetime.now(BEIJING_TZ).weekday()]

    content = BRIEF_HEADER
    content += f"## 📅 {date} {weekday}\n\n"

    # === 今日焦点 ===
    focus = brief.get("focus", {})
    focus_idx = focus.get("index", 0)
    if isinstance(focus_idx, int) and 0 <= focus_idx < len(candidates):
        focus_item = candidates[focus_idx]
        star = _importance_star(focus_item)
        content += f"### 📌 今日焦点\n\n"
        content += f"**[{_display_title(focus, focus_item)}]({focus_item['url']})** · `{focus_item['source']}`{star}\n\n"
        content += f"> {focus.get('editorial', '')}\n\n"
        content += _format_related(focus_item)
        content += "\n---\n\n"

    # === 热点速览 ===
    highlights = brief.get("highlights", [])
    if highlights:
        content += f"### 🔥 热点速览\n\n"
        for num, hl in enumerate(highlights, 1):
            idx = hl.get("index", 0)
            if isinstance(idx, int) and 0 <= idx < len(candidates):
                item = candidates[idx]
                editorial = hl.get("editorial", "")
                star = _importance_star(item)

                content += f"**{num}. [{_display_title(hl, item)}]({item['url']})** · `{item['source']}`{star}\n\n"
                if editorial:
                    content += f"{editorial}\n\n"
                related_text = _format_related(item)
                if related_text:
                    content += related_text
        content += "---\n\n"

    # === 今日工具 (skip items already in focus/highlights) ===
    tools = brief.get("tools", [])
    used_indices = {focus.get("index", -1)}
    for hl in highlights:
        used_indices.add(hl.get("index", -1))
    tools_to_show = [t for t in tools if t.get("index", -1) not in used_indices]
    if tools_to_show:
        content += f"### 🛠️ 今日工具\n\n"
        for tool in tools_to_show:
            idx = tool.get("index", 0)
            if isinstance(idx, int) and 0 <= idx < len(candidates):
                item = candidates[idx]
                reason = tool.get("reason", "")
                content += f"**[{_display_title(tool, item)}]({item['url']})** · `{item['source']}`\n\n"
                if reason:
                    content += f"{reason}\n\n"
        content += "---\n\n"

    content += BRIEF_FOOTER
    return content


def write_readme(content: str, output_path: str = "README.md"):
    """Write README.md."""
    Path(output_path).write_text(content, encoding="utf-8")
    logger.info(f"README written to {output_path}")


def write_archive(curation_result: dict, config: dict):
    """Write daily archive file with full brief + raw data."""
    if not config.get("enabled", True):
        return

    directory = config.get("directory", "archives")
    date = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
    weekday_zh = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    weekday = weekday_zh[datetime.now(BEIJING_TZ).weekday()]

    Path(directory).mkdir(parents=True, exist_ok=True)

    candidates = curation_result["candidates"]
    brief = curation_result["brief"]

    content = f"# AI Daily Brief — {date} {weekday}\n\n"
    content += f"> Generated at {datetime.now(BEIJING_TZ).strftime('%Y-%m-%d %H:%M CST')}\n\n"
    content += f"候选条目: {len(candidates)} | "

    highlights = brief.get("highlights", [])
    content += f"精选: {1 + len(highlights) + len(brief.get('tools', []))} 条\n\n"

    # Focus
    focus = brief.get("focus", {})
    focus_idx = focus.get("index", 0)
    if isinstance(focus_idx, int) and 0 <= focus_idx < len(candidates):
        item = candidates[focus_idx]
        content += f"## 📌 焦点: [{_display_title(focus, item)}]({item['url']})\n\n"
        content += f"{focus.get('editorial', '')}\n\n"

    # Highlights
    content += f"## 🔥 速览\n\n"
    for hl in highlights:
        idx = hl.get("index", 0)
        if isinstance(idx, int) and 0 <= idx < len(candidates):
            item = candidates[idx]
            content += f"### [{_display_title(hl, item)}]({item['url']})\n\n"
            content += f"- **Source**: {item['source']}\n"
            content += f"- **编辑点评**: {hl.get('editorial', '')}\n\n"

    # Tools
    tools = brief.get("tools") or []
    if tools:
        content += "## 🛠️ 工具\n\n"
        for tool in tools:
            idx = tool.get("index", 0)
            if isinstance(idx, int) and 0 <= idx < len(candidates):
                item = candidates[idx]
                content += f"### [{_display_title(tool, item)}]({item['url']})\n\n"
                content += f"{tool.get('reason', '')}\n\n"

    content += f"<details>\n<summary>完整候选与内部评分（{len(candidates)} 条）</summary>\n\n"
    for i, item in enumerate(candidates):
        content += f"{i+1}. [{item['title']}]({item['url']}) — {item['source']} "
        content += f"(importance: {item.get('importance', 5)}, topic: {item.get('topic_key', '')})\n"
    content += "\n</details>\n"

    path = Path(directory) / f"{date}.md"
    path.write_text(content, encoding="utf-8")
    logger.info(f"Archive written to {path}")


async def push_to_notion(items: list[dict], config: dict):
    """Push curated items to Notion database (optional)."""
    if not config.get("enabled", False):
        return

    token = os.environ.get("NOTION_TOKEN")
    db_id = os.environ.get(config.get("database_id_env", "NOTION_DATABASE_ID"))

    if not token or not db_id:
        logger.warning("Notion integration enabled but missing NOTION_TOKEN or database ID")
        return

    import httpx
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        for item in items[:20]:
            try:
                page = {
                    "parent": {"database_id": db_id},
                    "properties": {
                        "Title": {"title": [{"text": {"content": item["title"][:100]}}]},
                        "URL": {"url": item["url"]},
                        "Source": {"select": {"name": item["source"]}},
                        "Category": {"select": {"name": item.get("category", "tool")}},
                        "Importance": {"number": item.get("importance", 5)},
                        "Date": {"date": {"start": item["published"][:10]}},
                    },
                }
                resp = await client.post(
                    "https://api.notion.com/v1/pages",
                    headers=headers,
                    json=page,
                )
                if resp.status_code != 200:
                    logger.warning(f"Notion push failed for '{item['title'][:30]}': {resp.status_code}")
            except Exception as e:
                logger.warning(f"Notion push error: {e}")

    logger.info(f"Notion: pushed {min(len(items), 20)} items")
