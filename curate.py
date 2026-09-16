"""Convert generic news candidates into variable-count edition events."""

from __future__ import annotations

from copy import deepcopy
from typing import Mapping

from sources import NewsItem
from edition import merge_events


def event_from_news_item(item: NewsItem, event_id: str) -> dict:
    fact = (item.summary or item.title).strip()
    return {
        "id": event_id,
        "placement": "story",
        "window": "in_window",
        "title": item.title.strip(),
        "kicker": item.source,
        "facts": [fact],
        "conditions": [],
        "background": [],
        "sources": [
            {
                "label": item.source,
                "url": item.url,
                "kind": "primary",
                "published_at": item.published.isoformat(),
                "verified": False,
                "note": None,
            }
        ],
    }


def assemble_events(
    items: list[NewsItem],
    supplemental_events: list[dict] | None = None,
) -> list[dict]:
    events = [
        event_from_news_item(item, f"public-{index + 1}")
        for index, item in enumerate(items)
        if item.title.strip() and item.url.strip()
    ]
    events.extend(supplemental_events or [])
    return merge_events(events)


def _selection_rows(brief: dict) -> list[tuple[str, dict]]:
    rows: list[tuple[str, dict]] = []
    focus = brief.get("focus")
    if isinstance(focus, dict):
        rows.append(("lead", focus))
    rows.extend(("story", item) for item in brief.get("highlights", []))
    rows.extend(("desk", item) for item in brief.get("tools", []))
    return rows


def _candidate_event(candidate: dict, selection: dict, placement: str, event_id: str) -> dict:
    editorial = selection.get("editorial") or selection.get("reason")
    sources = [
        {
            "label": candidate["source"],
            "url": candidate["url"],
            "kind": "primary",
            "published_at": candidate.get("published"),
            "verified": bool(candidate.get("verified", False)),
            "note": candidate.get("verification_note"),
        }
    ]
    for related in candidate.get("related_sources", []):
        sources.append(
            {
                "label": related["source"],
                "url": related["url"],
                "kind": "secondary",
                "published_at": related.get("published"),
                "verified": bool(related.get("verified", False)),
                "note": related.get("verification_note"),
            }
        )
    return {
        "id": event_id,
        "placement": placement,
        "window": "in_window",
        "title": selection.get("title_zh") or candidate["title"],
        "kicker": candidate["source"],
        "facts": [candidate.get("summary") or candidate["title"]],
        "conditions": [],
        "background": [editorial] if editorial else [],
        "sources": sources,
    }


def assemble_curated_events(
    curation_result: dict,
    event_overrides_by_url: Mapping[str, dict] | None = None,
) -> list[dict]:
    """Build only the editor-selected events; the selection count stays data-driven."""
    candidates = curation_result.get("candidates", [])
    brief = curation_result.get("brief", {})
    overrides = event_overrides_by_url or {}

    events = []
    for order, (placement, selection) in enumerate(_selection_rows(brief), 1):
        index = selection.get("index")
        if type(index) is not int or not 0 <= index < len(candidates):
            continue
        candidate = candidates[index]
        event_id = f"event-{order}"
        override = overrides.get(candidate["url"].rstrip("/").lower())
        if override:
            event = deepcopy(override)
            event["id"] = event_id
            event["placement"] = placement
            event["title"] = selection.get("title_zh") or event["title"]
            editorial = selection.get("editorial") or selection.get("reason")
            if editorial:
                event["background"].append(editorial)
        else:
            event = _candidate_event(candidate, selection, placement, event_id)
        events.append(event)
    return merge_events(events)
