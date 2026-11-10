"""Structural checks for a new or changed edition.

``relationship``, ``reading``, and ``excerpt`` are values the caller recorded.
A non-empty excerpt does not prove that the excerpt supports the claim, and
``original`` does not prove that the page is the original report. Generation
code may store one bounded read with ``record_reading``. Deciding that the
excerpt supports the sentence is an editorial check. CI does neither. It
checks the shape of the record.

A claim marked ``predicted`` must show its value and a prediction word in the
same reader field. A claim marked ``measured`` is not reclassified. That gap
stays with the editor.
"""

from __future__ import annotations

import json
import re
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
PREDICTION_MARKERS = ("预测", "预计", "predicted", "forecast", "projected")
_NEGATED_PREDICTION = re.compile(
    r"不是预测|并非预测|不是预计|并非预计|不是projected|并非projected|not a prediction|not predicted",
    re.IGNORECASE,
)
_NUMERIC_UNIT = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|％|亿美元|万美元|亿元|万元|美元|元|亿|万|token|tokens|倍|个百分点)"
)
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_LATIN = re.compile(r"[A-Za-z]{4,}")
MIN_EXCERPT_CHARS = 8
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


def parse_editorial_verification(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise EditionError("event.editorial_verification must be an object")
    verdict = value.get("verdict")
    if verdict not in {"supports", "does_not_support"}:
        raise EditionError("event.editorial_verification.verdict is invalid")
    attempt = value.get("attempt")
    if type(attempt) is not int or attempt < 1:
        raise EditionError("event.editorial_verification.attempt must be an integer >= 1")
    return {
        "edition_id": _nonempty(value.get("edition_id"), "editorial_verification.edition_id"),
        "attempt": attempt,
        "source_url": _nonempty(value.get("source_url"), "editorial_verification.source_url"),
        "excerpt": _nonempty(value.get("excerpt"), "editorial_verification.excerpt"),
        "claim": _nonempty(value.get("claim"), "editorial_verification.claim"),
        "verdict": verdict,
    }


def _nonempty(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EditionError(f"{label} must be a non-empty string")
    return value.strip()


def assess_new_candidate(document: Mapping[str, Any]) -> dict[str, Any]:
    events = document.get("events") or []
    retained: list[str] = []
    rejected: list[dict[str, Any]] = []
    if not isinstance(events, list):
        raise EditionError("events must be a list")
    edition_id = document.get("edition_id") if isinstance(document.get("edition_id"), str) else ""
    attempt = document.get("attempt") if type(document.get("attempt")) is int else None
    for event in events:
        if not isinstance(event, Mapping):
            raise EditionError("event must be an object")
        event_id = event.get("id")
        if not isinstance(event_id, str) or not event_id.strip():
            raise EditionError("event.id must be a non-empty string")
        reasons = _event_reasons(event, edition_id, attempt)
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


def review_changed_edition(
    baseline: str | None,
    current: str,
    *,
    allow_published_correction: bool = False,
) -> str:
    matches = baseline is not None and baseline == current
    if baseline is not None and not matches and not allow_published_correction:
        raise EditionError("published edition is frozen")
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


def _event_reasons(event: Mapping[str, Any], edition_id: str, attempt: int | None) -> list[str]:
    reasons: list[str] = []
    sources = event.get("sources") or []
    saw_support = False
    good_original = False
    good_excerpt = ""
    good_url = ""
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
                if len(excerpt.strip()) < MIN_EXCERPT_CHARS:
                    reasons.append("excerpt_too_short")
                elif not _excerpt_shares_claim(event, excerpt):
                    reasons.append("excerpt_unrelated")
                else:
                    good_original = True
                    good_excerpt = excerpt.strip()
                    good_url = source.get("url") if isinstance(source.get("url"), str) else ""
            else:
                blocked.append(reading if isinstance(reading, str) else "missing")
    if not good_original:
        if not saw_support:
            reasons.append("missing_support")
        elif blocked:
            for reading in blocked:
                reasons.append(f"original_not_read:{reading}")
        elif "excerpt_too_short" not in reasons and "excerpt_unrelated" not in reasons:
            reasons.append("no_read_original")
    claims = event.get("claims") or []
    if _has_numeric_claim(event) and not claims:
        reasons.append("numeric_claim_without_kind")
    if isinstance(claims, list):
        for claim in claims:
            if not isinstance(claim, Mapping):
                continue
            kind = claim.get("kind")
            if kind == "predicted" and not _prediction_visible(event, claim):
                reasons.append("prediction_not_visible")
            if kind == "measured" and _measured_uses_prediction_word(event, claim):
                reasons.append("measured_describes_prediction")
    if good_original and not _verification_supports(
        event, edition_id, attempt, good_url, good_excerpt
    ):
        reasons.append("missing_editorial_verification")
    return list(dict.fromkeys(reasons))


def _field_shows_prediction(text: str, value: str) -> bool:
    if value not in text:
        return False
    return any(marker in _NEGATED_PREDICTION.sub("", text) for marker in PREDICTION_MARKERS)


def _has_numeric_claim(event: Mapping[str, Any]) -> bool:
    return bool(_NUMERIC_UNIT.search(_claim_text(event)))


def _claim_text(event: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for field in ("title", "kicker", "lede", "caveat"):
        value = event.get(field)
        if isinstance(value, str):
            parts.append(value)
    for field in ("facts", "conditions", "background"):
        items = event.get(field) or []
        if isinstance(items, list):
            parts.extend(item for item in items if isinstance(item, str))
    return "\n".join(parts)


def _excerpt_shares_claim(event: Mapping[str, Any], excerpt: str) -> bool:
    claim = _claim_text(event)
    if set(_NUMBER.findall(claim)) & set(_NUMBER.findall(excerpt)):
        return True
    latin_claim = {word.lower() for word in _LATIN.findall(claim)}
    latin_excerpt = {word.lower() for word in _LATIN.findall(excerpt)}
    if latin_claim & latin_excerpt:
        return True
    return bool(_cjk_bigrams(claim) & _cjk_bigrams(excerpt))


def _cjk_bigrams(text: str) -> set[str]:
    chars = [char for char in text if "\u4e00" <= char <= "\u9fff"]
    return {"".join(chars[index:index + 2]) for index in range(len(chars) - 1)}


def _measured_uses_prediction_word(event: Mapping[str, Any], claim: Mapping[str, Any]) -> bool:
    value = claim.get("value")
    if not isinstance(value, str) or not value:
        return False
    for text in _reader_fields(event):
        if value in text and _field_shows_prediction(text, value):
            return True
    return False


def _reader_fields(event: Mapping[str, Any]) -> list[str]:
    texts: list[str] = []
    for field in ("title", "kicker", "lede", "caveat"):
        value = event.get(field)
        if isinstance(value, str):
            texts.append(value)
    for field in ("facts", "conditions", "background"):
        items = event.get(field) or []
        if isinstance(items, list):
            texts.extend(item for item in items if isinstance(item, str))
    return texts


def _verification_supports(
    event: Mapping[str, Any],
    edition_id: str,
    attempt: int | None,
    source_url: str,
    excerpt: str,
) -> bool:
    records = event.get("editorial_verifications") or []
    if not isinstance(records, list):
        return False
    claim = _claim_text(event)
    for record in records:
        if not isinstance(record, Mapping):
            continue
        if record.get("verdict") != "supports":
            continue
        if record.get("edition_id") != edition_id or record.get("attempt") != attempt:
            continue
        if record.get("source_url") != source_url or record.get("excerpt") != excerpt:
            continue
        named = record.get("claim")
        if not isinstance(named, str) or named not in claim:
            continue
        return True
    return False


def _prediction_visible(event: Mapping[str, Any], claim: Mapping[str, Any]) -> bool:
    value = claim.get("value")
    if not isinstance(value, str) or not value:
        return False
    for field in ("title", "kicker", "lede", "caveat"):
        text = event.get(field)
        if isinstance(text, str) and _field_shows_prediction(text, value):
            return True
    for field in ("facts", "conditions", "background"):
        items = event.get(field) or []
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, str) and _field_shows_prediction(item, value):
                return True
    return False


def _rejection_message(report: Mapping[str, Any]) -> str:
    parts = [
        f"{item['id']}: {','.join(item['reasons'])}"
        for item in report["rejected"]
    ]
    retained = ",".join(report["retained"]) or "-"
    return f"new candidate rejected [{'; '.join(parts)}] retained [{retained}]"
