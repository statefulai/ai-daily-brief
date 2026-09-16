"""Adapt verified Grok Bot contributions to the generic local curation contract."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from edition import EditionError
from sources import NewsItem

SENSITIVITY_VALUES = ("public", "internal", "restricted")


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EditionError(f"{label} must be a non-empty string")
    return value.strip()


def _require_datetime(value: Any, label: str) -> str:
    text = _require_text(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EditionError(f"{label} must be an ISO 8601 datetime") from exc
    if parsed.tzinfo is None:
        raise EditionError(f"{label} must include a timezone")
    return text


def validate_contribution(record: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        raise EditionError("contribution must be an object")
    primary = record.get("primary_source")
    if not isinstance(primary, Mapping):
        raise EditionError("contribution.primary_source must be an object")
    facts = record.get("verified_facts")
    conditions = record.get("conditions", [])
    if not isinstance(facts, list) or not facts:
        raise EditionError("contribution.verified_facts must be a non-empty list")
    if not isinstance(conditions, list):
        raise EditionError("contribution.conditions must be a list")
    sensitivity = _require_text(record.get("sensitivity"), "contribution.sensitivity")
    if sensitivity not in SENSITIVITY_VALUES:
        raise EditionError(f"unsupported contribution sensitivity: {sensitivity}")
    flash_ref = record.get("flash_ref")
    if flash_ref is not None and (not isinstance(flash_ref, str) or not flash_ref.strip()):
        raise EditionError("contribution.flash_ref must be a non-empty string when present")
    return {
        "event": _require_text(record.get("event"), "contribution.event"),
        "primary_source": {
            "title": _require_text(primary.get("title"), "contribution.primary_source.title"),
            "url": _require_text(primary.get("url"), "contribution.primary_source.url"),
            "published_at": primary.get("published_at") or None,
        },
        "source_time": _require_datetime(record.get("source_time"), "contribution.source_time"),
        "verified_facts": [
            _require_text(item, "contribution.verified_fact") for item in facts
        ],
        "conditions": [
            _require_text(item, "contribution.condition") for item in conditions
        ],
        "flash_ref": flash_ref.strip() if isinstance(flash_ref, str) else None,
        "sensitivity": sensitivity,
    }


def validate_contributions(records: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(records, list):
        raise EditionError("contributions must be a list")
    return [validate_contribution(item) for item in records]


def extend_curation_items(items: list[NewsItem], records: list[dict]) -> list[NewsItem]:
    """Return a copy with public, verified contributions added as ordinary candidates."""
    combined = list(items)
    for raw in records:
        contribution = validate_contribution(raw)
        if contribution["sensitivity"] != "public":
            continue
        combined.append(
            NewsItem(
                title=contribution["event"],
                url=contribution["primary_source"]["url"],
                source=contribution["primary_source"]["title"],
                summary=" ".join(contribution["verified_facts"]),
                published=datetime.fromisoformat(
                    contribution["source_time"].replace("Z", "+00:00")
                ),
            )
        )
    return combined


def event_overrides(records: list[dict]) -> dict[str, dict]:
    """Preserve verified facts if a contribution is selected by the generic curator."""
    overrides: dict[str, dict] = {}
    for raw in records:
        contribution = validate_contribution(raw)
        if contribution["sensitivity"] != "public":
            continue
        source = contribution["primary_source"]
        event = {
            "id": "contribution",
            "placement": "story",
            "window": "in_window",
            "title": contribution["event"],
            "kicker": source["title"],
            "facts": contribution["verified_facts"],
            "conditions": contribution["conditions"],
            "background": [],
            "sources": [
                {
                    "label": source["title"],
                    "url": source["url"],
                    "kind": "primary",
                    "published_at": source["published_at"] or contribution["source_time"],
                    "verified": True,
                    "note": "由可选供稿适配层标记为已核对事实",
                }
            ],
        }
        overrides[source["url"].rstrip("/").lower()] = event
    return overrides
