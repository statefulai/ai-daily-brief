"""Claim support for a new or changed edition.

A stored ``verified`` flag and a page timestamp record access. They do not
record that a page supports a claim. Whether the excerpt supports the
sentence, whether the page is the original report, whether a number is a
prediction, and whether the item is worth publishing stay with the editor.
This module checks that the recorded relationship, reading result, and
reader-visible prediction label are consistent. It does not fetch pages.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from edition import EditionError

READINGS = frozenset(
    {
        "read",
        "not_fetched",
        "http_403",
        "http_429",
        "timeout",
        "timestamps_only",
        "other_failure",
    }
)
RELATIONSHIPS = frozenset({"original", "retelling", "supporting"})
CLAIM_KINDS = frozenset({"measured", "predicted", "attributed"})
PREDICTION_MARKERS = ("预测", "预计", "predicted", "forecast")
READER_FIELDS = ("title", "kicker", "lede", "caveat", "facts", "conditions", "background")
MAX_REVISION = 2


def record_reading(
    *,
    http_status: int | None = None,
    error: str | None = None,
    body_read: bool = False,
    timestamp_only: bool = False,
) -> str:
    """Map one bounded read to a reading result. This function does not fetch."""
    if error and "timeout" in error.lower():
        return "timeout"
    if http_status == 403:
        return "http_403"
    if http_status == 429:
        return "http_429"
    if http_status is not None and http_status >= 400:
        return "other_failure"
    if timestamp_only and not body_read:
        return "timestamps_only"
    if body_read:
        return "read"
    return "not_fetched"


def parse_support(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise EditionError("event.source.support must be an object")
    relationship = value.get("relationship")
    reading = value.get("reading")
    if relationship not in RELATIONSHIPS:
        raise EditionError("event.source.support.relationship is invalid")
    if reading not in READINGS:
        raise EditionError("event.source.support.reading is invalid")
    excerpt = value.get("excerpt")
    if excerpt is not None and (not isinstance(excerpt, str) or not excerpt.strip()):
        raise EditionError("event.source.support.excerpt must be a non-empty string")
    return {
        "relationship": relationship,
        "reading": reading,
        "excerpt": excerpt.strip() if isinstance(excerpt, str) else None,
    }


def parse_claim(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise EditionError("event.claim must be an object")
    kind = value.get("kind")
    text = value.get("value")
    if kind not in CLAIM_KINDS:
        raise EditionError("event.claim.kind is invalid")
    if not isinstance(text, str) or not text.strip():
        raise EditionError("event.claim.value must be a non-empty string")
    return {"kind": kind, "value": text.strip()}


def enforcement_mode(*, changed: bool, matches_baseline: bool) -> str:
    """Historical files stay on the old gate. A new draft cannot opt into it."""
    if matches_baseline and not changed:
        return "historical"
    return "new"


def assess_new_candidate(document: Mapping[str, Any]) -> dict[str, Any]:
    events = document.get("events") or []
    retained: list[str] = []
    rejected: list[dict[str, Any]] = []
    if not isinstance(events, list):
        raise EditionError("events must be a list")
    for event in events:
        if not isinstance(event, Mapping):
            raise EditionError("event must be an object")
        event_id = event.get("id")
        if not isinstance(event_id, str) or not event_id.strip():
            raise EditionError("event.id must be a non-empty string")
        reasons = _event_reasons(event)
        if reasons:
            rejected.append({"id": event_id, "reasons": reasons})
        else:
            retained.append(event_id)
    return {"retained": retained, "rejected": rejected}


def require_new_candidate(document: Mapping[str, Any]) -> dict[str, Any]:
    report = assess_new_candidate(document)
    if report["rejected"]:
        raise EditionError(_rejection_message(report))
    return report


def review_changed_edition(baseline: str | None, current: str) -> str:
    matches = baseline is not None and baseline == current
    mode = enforcement_mode(changed=not matches, matches_baseline=matches)
    if mode == "new":
        require_new_candidate(json.loads(current))
    return mode


def evidence_outcome(
    source_records: list[Mapping[str, Any]],
    *,
    retained: int,
    rejected: int,
) -> str:
    if rejected and retained == 0:
        return "failed"
    if retained:
        return "published_candidate"
    if any(item.get("status") == "failed" for item in source_records):
        return "failed"
    return "no_new_value"


def evidence_failure(rejected: list[Mapping[str, Any]]) -> dict[str, str]:
    ids = ", ".join(str(item["id"]) for item in rejected)
    return {
        "stage": "evidence",
        "message": f"evidence rejected every event: {ids}",
    }


def _event_reasons(event: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    sources = event.get("sources") or []
    saw_support = False
    good_original = False
    blocked: list[str] = []
    if isinstance(sources, list):
        for source in sources:
            if not isinstance(source, Mapping):
                continue
            support = source.get("support")
            if not isinstance(support, Mapping):
                continue
            saw_support = True
            if support.get("relationship") != "original":
                continue
            reading = support.get("reading")
            excerpt = support.get("excerpt")
            if reading == "read" and isinstance(excerpt, str) and excerpt.strip():
                good_original = True
            else:
                blocked.append(reading if isinstance(reading, str) else "missing")
    if not good_original:
        if not saw_support:
            reasons.append("missing_support")
        elif blocked:
            for reading in blocked:
                reasons.append(f"original_not_read:{reading}")
        else:
            reasons.append("no_read_original")
    for claim in event.get("claims") or []:
        if isinstance(claim, Mapping) and claim.get("kind") == "predicted":
            if not _prediction_visible(event, claim):
                reasons.append("prediction_not_visible")
    return list(dict.fromkeys(reasons))


def _reader_text(event: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for field in READER_FIELDS:
        value = event.get(field)
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            parts.extend(item for item in value if isinstance(item, str))
    return "\n".join(parts)


def _prediction_visible(event: Mapping[str, Any], claim: Mapping[str, Any]) -> bool:
    visible = _reader_text(event)
    value = claim.get("value")
    if not isinstance(value, str) or value not in visible:
        return False
    return any(marker in visible for marker in PREDICTION_MARKERS)


def _rejection_message(report: Mapping[str, Any]) -> str:
    parts = [
        f"{item['id']}: {','.join(item['reasons'])}"
        for item in report["rejected"]
    ]
    retained = ",".join(report["retained"]) or "-"
    return f"new candidate rejected [{'; '.join(parts)}] retained [{retained}]"
