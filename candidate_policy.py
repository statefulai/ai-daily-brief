"""Structural checks for a new or changed edition.

``relationship``, ``reading``, and ``excerpt`` are values the caller recorded.
A non-empty excerpt does not prove that the excerpt supports the claim, and
``original`` does not prove that the page is the original report. Generation
code may store one bounded read with ``record_reading``. Deciding that the
excerpt supports the sentence is an editorial check. CI does neither. It
checks the shape of the record.

A claim marked ``predicted`` must show its value and an unnegated prediction
word in the same reader field. Negation is the eight characters before the
word, not a fixed phrase list. A measured claim is rejected when that same
field uses a prediction word. An excerpt that says projected while the reader
text says the number was measured still passes. That gap stays with the editor.
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
_PREDICTION_WORD = re.compile(r"预测|预计|predicted|forecast|projected", re.IGNORECASE)
_NEGATION_IN_WINDOW = re.compile(r"不|非|没|无|\bnot\b|\bno\b", re.IGNORECASE)
_UNIT_NUMBER = re.compile(
    r"\$(\d+(?:\.\d+)?)[BMK]?"
    r"|(\d+(?:\.\d+)?)\s*(?:billion|million|tokens|token|ms|GB|TB|MB|%|％)"
    r"|(\d+(?:\.\d+)?)\s*(?:亿美元|万美元|亿元|万元|美元|元|亿|万|倍|个百分点|分|名|家|人)"
    r"|(\d+(?:\.\d+)?)(?:ms|GB|TB|MB|[xX])",
    re.IGNORECASE,
)
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_YEAR = re.compile(r"(?:19|20)\d{2}")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_LATIN_WORD = re.compile(r"[A-Za-z]{5,}")
MIN_EXCERPT_CHARS = 12
MIN_SHARED_CJK = 6
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
                elif _excerpt_copies_text(event, excerpt):
                    reasons.append("excerpt_copies_text")
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
        elif (
            "excerpt_too_short" not in reasons
            and "excerpt_unrelated" not in reasons
            and "excerpt_copies_text" not in reasons
        ):
            reasons.append("no_read_original")
    claims = event.get("claims") or []
    reasons.extend(_claim_coverage_reasons(event, claims))
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
    """A prediction word counts only when no negation sits in the preceding window."""
    if value not in text:
        return False
    for match in _PREDICTION_WORD.finditer(text):
        window = text[max(0, match.start() - 8):match.start()]
        if _NEGATION_IN_WINDOW.search(window):
            continue
        return True
    return False


def _unit_numbers(text: str) -> set[str]:
    found: set[str] = set()
    for match in _UNIT_NUMBER.finditer(text):
        number = next(group for group in match.groups() if group)
        if _YEAR.fullmatch(number) and re.search(rf"{re.escape(number)}\s*年", text):
            continue
        found.add(number)
    return found


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


def _claim_coverage_reasons(event: Mapping[str, Any], claims: Any) -> list[str]:
    reasons: list[str] = []
    text = _claim_text(event)
    needed = _unit_numbers(text)
    values = []
    if isinstance(claims, list):
        values = [
            item.get("value")
            for item in claims
            if isinstance(item, Mapping) and isinstance(item.get("value"), str)
        ]
    if needed and not values:
        reasons.append("numeric_claim_without_kind")
    for number in sorted(needed):
        if values and not any(number in value for value in values):
            reasons.append("numeric_claim_uncovered")
            break
    for value in values:
        if value not in text:
            reasons.append("claim_value_not_in_text")
            break
    return reasons


def _excerpt_copies_text(event: Mapping[str, Any], excerpt: str) -> bool:
    copied = excerpt.strip()
    if len(copied) < MIN_EXCERPT_CHARS:
        return False
    for field in _reader_fields(event):
        text = field.strip()
        if not text:
            continue
        if copied == text:
            return True
        if len(text) >= MIN_EXCERPT_CHARS and (copied in text or text in copied):
            return True
    return False


def _excerpt_shares_claim(event: Mapping[str, Any], excerpt: str) -> bool:
    claim = _claim_text(event)
    claim_numbers = {number for number in _NUMBER.findall(claim) if not _YEAR.fullmatch(number)}
    excerpt_numbers = {number for number in _NUMBER.findall(excerpt) if not _YEAR.fullmatch(number)}
    if claim_numbers & excerpt_numbers:
        return True
    if _shared_cjk_length(claim, excerpt) >= MIN_SHARED_CJK:
        return True
    latin_claim = {word.lower() for word in _LATIN_WORD.findall(claim)}
    latin_excerpt = {word.lower() for word in _LATIN_WORD.findall(excerpt)}
    return len(latin_claim & latin_excerpt) >= 2


def _shared_cjk_length(left: str, right: str) -> int:
    best = 0
    for run in _CJK_RUN.findall(left):
        for other in _CJK_RUN.findall(right):
            best = max(best, _common_run(run, other))
    return best


def _common_run(left: str, right: str) -> int:
    best = 0
    for start in range(len(left)):
        for offset in range(len(right)):
            length = 0
            while (
                start + length < len(left)
                and offset + length < len(right)
                and left[start + length] == right[offset + length]
            ):
                length += 1
            best = max(best, length)
    return best


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
        if not _substantive_claim(named, claim):
            continue
        return True
    return False


def _substantive_claim(named: str, text: str) -> bool:
    token = named.strip()
    if len(token) < 4 or token in {"焦点", "。", "，"}:
        return False
    numbers = {number for number in _NUMBER.findall(text) if not _YEAR.fullmatch(number)}
    if any(number in token for number in numbers):
        return True
    return len(token) >= 6 and _shared_cjk_length(token, text) >= 4


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
