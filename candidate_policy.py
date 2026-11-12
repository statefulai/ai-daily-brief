"""Structural checks for a new or changed edition.

``relationship``, ``reading``, and ``excerpt`` are values the caller recorded.
A non-empty excerpt does not prove that the excerpt supports the claim, and
``original`` does not prove that the page is the original report. Generation
code may store one bounded read with ``record_reading``. Deciding that the
excerpt supports the sentence is an editorial check. CI does neither. It
checks the shape of the record.

A claim marked ``predicted`` must show its value and an unnegated prediction
word in the same reader field. Negation is the rest of that clause. A measured
claim is rejected when its own field uses a prediction word, and also when the
excerpt clause that contains the measured number uses one.
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
PREDICTION_MARKERS = (
    "预测",
    "预计",
    "预期",
    "预估",
    "估计",
    "有望",
    "将达",
    "计划",
    "目标",
    "predicted",
    "forecast",
    "forecasted",
    "projected",
    "expected",
    "estimate",
    "estimated",
    "guidance",
    "outlook",
    "target",
)
_PREDICTION_WORD = re.compile(
    r"预期|预估|预计|预测|估计|有望|将达|(?<![周月日年])计划(?![板书表性])|目标|"
    r"\b(?:forecasted|forecasting|forecasts|forecast|"
    r"projected|predictions|prediction|predicted|"
    r"estimated|estimates|estimate|"
    r"expected|expects|expect|"
    r"guidance|outlooks|outlook|"
    r"targets|targeted|targeting|target)\b",
    re.IGNORECASE,
)
_FALSE_NEGATION = (
    "不得不",
    "不仅",
    "不但",
    "不光",
    "未来",
    "非常",
    "无论",
    "不久",
    "不断",
    "无需",
    "非但",
)
_CLAUSE_NEGATION = re.compile(
    r"rather than|否认|尚未|从未|并不属于|不属于|并非|不是|并不|没有|"
    r"isn't|is not|not yet|never|without|别|勿|未|没|不|非|无|n't|\bnot\b|\bno\b",
    re.IGNORECASE,
)
_CLAUSE_BOUNDARY = "，。；、,;!！？?\n"
_PREDICTION_COMPOUND = ("条目标题", "目标检测", "目标模型", "目标函数")
_QUANTITY_UNIT = re.compile(
    r"(?:[KMGTPE]FLOPS|FLOPS|[KMGT]bps|bps|GHz|MHz|kHz|GW|MW|kW|PB|TB|GB|MB|nm|ms|W)\b",
    re.IGNORECASE,
)
_CN_EXCLUDE = (
    "零一万物",
    "千万不要",
    "百分比",
    "通义千问",
    "千问",
    "千帆",
    "万相",
    "万物",
    "十分",
    "二分",
    "一键",
)
_BOILERPLATE = re.compile(
    r"cookie|cookies|all rights reserved|privacy policy|版权所有",
    re.IGNORECASE,
)
_FW_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
_UNIT_AFTER = re.compile(
    r"\s*(?:TB/s|GB/s|fps|tokens|token|trillion|billion|million|ms|TB|GB|MB|美金|美元|分钟|小时|秒钟|秒|个百分点|%|％|千亿|万亿|千万|百亿|十亿|百万|亿元|万元|亿美元|万美元|亿|万|千|百|元|倍|分|名|家|人|[BMKxX])(?![A-Za-z])",
    re.IGNORECASE,
)
_ARABIC = re.compile(r"\d+(?:\.\d+)?")
_CN_NUMERAL = re.compile(r"[零〇一二三四五六七八九十百千万亿两]+")
_CN_FRACTION = re.compile(
    r"[零〇一二三四五六七八九十百千万亿两]+分之[零〇一二三四五六七八九十百千万亿两]+"
)
_CN_SINGLE_DIGIT = "零〇一二三四五六七八九两"
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
    parsed = {
        "edition_id": _nonempty(value.get("edition_id"), "editorial_verification.edition_id"),
        "attempt": attempt,
        "source_url": _nonempty(value.get("source_url"), "editorial_verification.source_url"),
        "excerpt": _nonempty(value.get("excerpt"), "editorial_verification.excerpt"),
        "claim": _nonempty(value.get("claim"), "editorial_verification.claim"),
        "verdict": verdict,
    }
    if "source_span" in value:
        parsed["source_span"] = _nonempty(value.get("source_span"), "editorial_verification.source_span")
    if "translated_claim" in value:
        parsed["translated_claim"] = _nonempty(
            value.get("translated_claim"), "editorial_verification.translated_claim"
        )
    if "normalized_numbers" in value:
        pairs = value.get("normalized_numbers")
        if not isinstance(pairs, list) or not pairs:
            raise EditionError("editorial_verification.normalized_numbers must be a non-empty list")
        parsed["normalized_numbers"] = [_normalized_pair(item) for item in pairs]
    return parsed


def _normalized_pair(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise EditionError("editorial_verification.normalized_numbers item must be an object")
    return {
        "source": _nonempty(value.get("source"), "normalized_numbers.source"),
        "claim": _nonempty(value.get("claim"), "normalized_numbers.claim"),
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
                elif not _excerpt_shares_claim(event, excerpt) and not _translation_bridge(
                    event, excerpt.strip()
                ):
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
    if _translation_pair_mismatch(event):
        reasons.append("normalized_quantity_mismatch")
    return list(dict.fromkeys(reasons))


def _field_shows_prediction(text: str, value: str) -> bool:
    """A prediction word counts when its own clause does not negate it.

    The clause runs to the nearest punctuation. Compounds such as 非常, 不久,
    and 无论 are not negations. A negation anywhere earlier in that clause,
    such as 并不属于…预测, does not count as visible.
    """
    if value not in text:
        return False
    for match in _PREDICTION_WORD.finditer(text):
        if not _clause_negates(text, match.start()):
            return True
    return False


def _clause_boundary(text: str, index: int) -> bool:
    char = text[index]
    if char in ".．" and 0 < index < len(text) - 1:
        if text[index - 1].isdigit() and text[index + 1].isdigit():
            return False
        return True
    return char in _CLAUSE_BOUNDARY


def _clause_negates(text: str, start: int) -> bool:
    left = 0
    for index, char in enumerate(text):
        if index < start and _clause_boundary(text, index):
            left = index + 1
    before = text[left:start]
    for word in _FALSE_NEGATION:
        before = before.replace(word, " " * len(word))
    return _CLAUSE_NEGATION.search(before) is not None


def _norm_digits(text: str) -> str:
    return text.translate(_FW_DIGITS)


def _unit_numbers(text: str) -> set[str]:
    """Arabic numbers are claims. Years, dates, versions, model names, and ids are not.

    Chinese numerals stay on the quantity rule, including the lone-digit
    relaxation for 个, 位, 次, 款, 名, 人, 年, and 种.
    """
    normalized = _norm_digits(text)
    found: set[str] = set()
    consumed: list[tuple[int, int]] = []
    for match in _ARABIC.finditer(normalized):
        number = match.group()
        if _arabic_is_exempt(normalized, match.start(), match.end(), number):
            continue
        found.add(number)
        unit = _UNIT_AFTER.match(normalized[match.end():])
        if unit is not None:
            consumed.append((match.end(), match.end() + unit.end()))
    for match in _CN_NUMERAL.finditer(normalized):
        if any(start <= match.start() and match.end() <= stop for start, stop in consumed):
            continue
        fraction = _covering_fraction(normalized, match.start(), match.end())
        if fraction is not None:
            found.add(fraction)
            continue
        token = match.group()
        if token == "百" and normalized[match.end():match.end() + 2] == "分之":
            continue
        if _inside_exclusion(normalized, match.start(), match.end()):
            continue
        prefix = normalized[max(0, match.start() - 3):match.start()] == "百分之"
        preceding = normalized[: match.start()]
        if prefix or _is_cn_quantity(token, normalized[match.end():], preceding):
            found.add("百分之" + token if prefix else token)
    return found


def _covering_fraction(text: str, start: int, end: int) -> str | None:
    for match in _CN_FRACTION.finditer(text):
        if match.start() <= start and end <= match.end():
            return match.group()
    return None


def _inside_exclusion(text: str, start: int, end: int) -> bool:
    for phrase in _CN_EXCLUDE:
        found = text.find(phrase)
        while found >= 0:
            if found <= start and end <= found + len(phrase):
                return True
            found = text.find(phrase, found + 1)
    return False


def _is_cn_quantity(token: str, following: str, preceding: str = "") -> bool:
    """Count a Chinese numeral when it is a magnitude or a measured unit.

    A lone digit before 个, 位, 次, 款, 名, 人, 年, or 种 is grammatical
    (一个, 一次, 第一, 另一家), not a claim. 家 counts only when the digit
    is not 一, so 六家 stays and 一家 does not. 倍, 成, currency, and clock
    units stay claims for every digit, including 两倍 and 三成.
    """
    digits = _CN_SINGLE_DIGIT
    magnitudes = "十百千万亿"
    has_digit = any(char in digits for char in token)
    has_magnitude = any(char in magnitudes for char in token)
    if has_digit and has_magnitude:
        return True
    if not has_digit and has_magnitude and len(token) >= 2:
        return True
    measured = re.match(r"\s*(?:倍|成|美元|美金|元|秒钟|秒|分钟|小时)", following) is not None
    counted = re.match(r"\s*(?:个|位|次|款|家|名|人|年|种)", following) is not None
    if token and set(token) <= set("一"):
        if preceding.endswith("第") or preceding.endswith(
            ("另", "上", "下", "又", "同", "唯")
        ) or preceding.endswith("任何"):
            return False
        if measured:
            return True
        return False
    if len(token) == 1 and token in digits:
        if preceding.endswith("第"):
            return False
        if measured:
            return True
        if token != "一" and re.match(r"\s*家", following):
            return True
        return False
    if (has_magnitude or (len(token) >= 2 and has_digit)) and (measured or counted):
        return True
    return _cn_unit_after(following) is not None and (has_digit or has_magnitude)


def _cn_unit_after(following: str):
    return re.match(r"\s*(?:美元|美金|元|秒钟|秒|分钟|小时|倍|成)", following)


_EN_MONTH = re.compile(
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December|"
    r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?\s+$",
    re.IGNORECASE,
)
_MONTH_DAY_YEAR = re.compile(
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December|"
    r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?\s+\d{1,2},?\s+$",
    re.IGNORECASE,
)


def _timezone_sign(text: str, start: int, end: int) -> bool:
    """+8 and +08:00 are offsets. +30% and -15% are quantities."""
    if start == 0 or text[start - 1] not in "+-":
        return False
    rest = text[end:]
    if rest.startswith(":") and len(rest) > 1 and rest[1].isdigit():
        return True
    prefix = text[max(0, start - 4):start - 1]
    return re.search(r"(?:UTC|GMT)$", prefix, re.IGNORECASE) is not None


def _labeled_year(text: str, start: int, end: int, number: str) -> bool:
    """A 19xx/20xx count is a claim unless it is a year label or a date."""
    if not _YEAR.fullmatch(number):
        return False
    rest = text[end:].lstrip()
    if rest.startswith(("财年", "年度", "年代", "年", "月", "日", "号")):
        return True
    window = text[max(0, start - 24):start]
    if _MONTH_DAY_YEAR.search(window) or _EN_MONTH.search(window):
        return True
    return re.search(r"[A-Za-z]\s+$", window) is not None


def _arabic_is_exempt(text: str, start: int, end: int, number: str) -> bool:
    """Dates, clocks, versions, model tokens, and labeled years are not claims."""
    if start > 0:
        previous = text[start - 1]
        if previous.isascii() and previous.isalpha():
            return True
        if previous == ":" or _timezone_sign(text, start, end):
            return True
        if previous == "-" and start > 1 and (
            (text[start - 2].isdigit() and len(number) == 2)
            or (text[start - 2].isascii() and text[start - 2].isalpha())
        ):
            return True
    if end < len(text) and text[end] == ":" and text[end + 1:end + 3].isdigit():
        return True
    if end < len(text) and text[end] == "." and end + 1 < len(text) and text[end + 1].isdigit():
        return True
    if start > 1 and text[start - 1] == "." and text[start - 2].isdigit():
        return True
    if _version_or_date(text, start, end) or _labeled_year(text, start, end, number):
        return True
    rest = text[end:].lstrip()
    if rest.startswith(("月", "日", "号")):
        return True
    if _EN_MONTH.search(text[max(0, start - 16):start]):
        return True
    if "." in number:
        after = text[end:].lstrip()
        if _QUANTITY_UNIT.match(after):
            return False
        if re.match(r"[A-Z][A-Za-z]+", after) or re.match(r"\d+(?:\.\d+)?\s*[BMK]\b", after):
            return True
    return False


def _version_or_date(text: str, start: int, end: int) -> bool:
    if start > 0 and text[start - 1] in "vV":
        return True
    if start > 0 and text[start - 1] == "." and end < len(text) and text[end] == ".":
        return True
    if end < len(text) and text[end] == "-" and re.match(r"\d{2}-\d{2}", text[end + 1:]):
        return True
    if start > 0 and text[start - 1] == "-" and re.match(r"\d{4}$", text[max(0, start - 5):start]):
        return True
    return False


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
    spans = [value for value in values if _is_numeric_span(value, needed, text)]
    if needed and not spans:
        reasons.append("numeric_claim_without_kind" if not values else "numeric_claim_uncovered")
    for number in sorted(needed):
        if spans and not any(_number_token(span, number) for span in spans):
            reasons.append("numeric_claim_uncovered")
            break
    for value in values:
        if _norm_digits(value) not in _norm_digits(text):
            reasons.append("claim_value_not_in_text")
            break
        if needed and not _is_numeric_span(value, needed, text):
            reasons.append("claim_value_not_a_span")
            break
    return list(dict.fromkeys(reasons))


def _is_numeric_span(value: str, needed: set[str], text: str) -> bool:
    if not needed:
        return True
    if len(value) > 32 or re.search(r"[，。；;！？!?]", value):
        return False
    hits = [number for number in needed if _number_token(value, number)]
    return len(hits) == 1 and _norm_digits(value) in _norm_digits(text)


def _number_token(value: str, number: str) -> bool:
    if re.fullmatch(r"\d+(?:\.\d+)?", number):
        return re.search(rf"(?<![\d.]){re.escape(number)}(?![\d.])", _norm_digits(value)) is not None
    return number in value


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


def _translation_pair_mismatch(event: Mapping[str, Any]) -> bool:
    """A stated pair is checked even when the excerpt and the lede share a digit."""
    records = event.get("editorial_verifications") or []
    if not isinstance(records, list):
        return False
    for record in records:
        if not isinstance(record, Mapping):
            continue
        pairs = record.get("normalized_numbers")
        if not pairs:
            continue
        if not isinstance(pairs, list):
            return True
        for item in pairs:
            if not isinstance(item, Mapping):
                return True
            source = item.get("source")
            claim = item.get("claim")
            if not isinstance(source, str) or not isinstance(claim, str):
                return True
            source_qty = _parse_quantity(source)
            claim_qty = _parse_quantity(claim)
            if (
                source_qty is None
                or claim_qty is None
                or source_qty[1] != claim_qty[1]
                or not _same_stated_quantity(source, claim, source_qty[0], claim_qty[0])
            ):
                return True
    return False


def _translation_bridge(event: Mapping[str, Any], excerpt: str) -> bool:
    """A named translation can stand in for token overlap. It does not prove the page.

    The verification must carry a verbatim source span and the translated claim.
    A cookie notice or rights line cannot use this path. Paired quantities must be
    equal after scaling, and each side must appear in its declared span. The pair
    is still an attestation that the sentences correspond.
    """
    records = event.get("editorial_verifications") or []
    if not isinstance(records, list):
        return False
    text = _claim_text(event)
    for record in records:
        if not isinstance(record, Mapping) or record.get("verdict") != "supports":
            continue
        span = record.get("source_span")
        translated = record.get("translated_claim")
        if not isinstance(span, str) or len(span) < MIN_EXCERPT_CHARS or span not in excerpt:
            continue
        if _BOILERPLATE.search(excerpt) or _BOILERPLATE.search(span):
            continue
        if not isinstance(translated, str) or translated not in text or not _substantive_claim(translated, text):
            continue
        pairs = record.get("normalized_numbers")
        if not _numbers_are_normalized(span, translated, text, pairs):
            continue
        return True
    return False


def _numbers_are_normalized(span: str, translated: str, text: str, pairs: Any) -> bool:
    if _BOILERPLATE.search(span) or _BOILERPLATE.search(translated):
        return False
    pair_list = pairs if isinstance(pairs, list) else []
    parsed: list[tuple[float, str, float]] = []
    for item in pair_list:
        if not isinstance(item, Mapping):
            return False
        source = item.get("source")
        claim = item.get("claim")
        if not isinstance(source, str) or not isinstance(claim, str):
            return False
        if source not in span or claim not in translated:
            return False
        source_qty = _parse_quantity(source)
        claim_qty = _parse_quantity(claim)
        if source_qty is None or claim_qty is None:
            return False
        if source_qty[1] != claim_qty[1] or not _same_stated_quantity(
            source, claim, source_qty[0], claim_qty[0]
        ):
            return False
        parsed.append((source_qty[0], source_qty[1], claim_qty[0]))
    for quantity in _quantities_in(text):
        if not any(_near(quantity[0], claim) and quantity[1] == kind for _, kind, claim in parsed):
            return False
    for quantity in _quantities_in(span):
        if not any(_near(quantity[0], source) and quantity[1] == kind for source, kind, _ in parsed):
            return False
    return True


_QUANTITY_SCALE = (
    ("千亿", 10**11),
    ("万亿", 10**12),
    ("千万", 10**7),
    ("百亿", 10**10),
    ("十亿", 10**9),
    ("百万", 10**6),
    ("trillion", 10**12),
    ("billion", 10**9),
    ("million", 10**6),
    ("thousand", 10**3),
    ("亿", 10**8),
    ("万", 10**4),
    ("千", 10**3),
)


def _near(left: float, right: float) -> bool:
    scale = max(abs(left), abs(right), 1.0)
    return abs(left - right) / scale <= 1e-9


def _coeff_decimals(text: str) -> int:
    raw = _norm_digits(text).replace(",", "").replace("，", "")
    match = re.search(r"\d+\.(\d+)", raw)
    return len(match.group(1)) if match else 0


def _written_scale(text: str, value: float) -> float:
    raw = _norm_digits(text).replace(",", "").replace("，", "")
    match = re.search(r"\d+(?:\.\d+)?", raw)
    if match is None or float(match.group()) == 0:
        return 1.0
    return value / float(match.group())


def _significant_digits(text: str) -> int:
    raw = _norm_digits(text).replace(",", "").replace("，", "")
    match = re.search(r"\d+(?:\.\d+)?", raw)
    if match is None:
        return 0
    token = match.group()
    if "." in token:
        digits = token.replace(".", "").lstrip("0")
        return len(digits)
    return len(token.lstrip("0") or "0")


def _same_stated_quantity(left_text: str, right_text: str, left_val: float, right_val: float) -> bool:
    """Match a coarser written number only when it is the finer one rounded to that precision.

    Two equal values match, including 1,000,000 and 100 万. A rounded match
    requires the coarser side to keep at least two significant digits, so 1.48
    matches 1.5 and 1.5 does not match 2. Two integers such as 1 million and
    102 万 must be the same value.
    """
    if _near(left_val, right_val):
        return True
    left_places = _coeff_decimals(left_text)
    right_places = _coeff_decimals(right_text)
    if left_places == right_places:
        return False
    if left_places < right_places:
        coarse_text, coarse_val, fine_val, places = left_text, left_val, right_val, left_places
    else:
        coarse_text, coarse_val, fine_val, places = right_text, right_val, left_val, right_places
    if _significant_digits(coarse_text) < 2:
        return False
    scale = _written_scale(coarse_text, coarse_val)
    return _near(round(fine_val / scale, places), coarse_val / scale)


def _quantity_kind(text: str) -> str:
    lowered = _norm_digits(text).lower()
    if re.search(r"个百分点|percentage\s+points?", lowered):
        return "percentage_point"
    if re.search(r"%|％|百分之|\bpercent\b", lowered):
        return "percent"
    return "absolute"


def _parse_quantity(text: str) -> tuple[float, str] | None:
    raw = _norm_digits(text)
    kind = _quantity_kind(raw)
    body = raw.replace(",", "").replace("，", "").replace("百分之", "")
    fraction = re.search(
        r"([零〇一二三四五六七八九十百千万亿两]+)分之([零〇一二三四五六七八九十百千万亿两]+)",
        body,
    )
    if fraction is not None:
        denominator = _cn_to_float(fraction.group(1))
        numerator = _cn_to_float(fraction.group(2))
        if denominator not in (None, 0) and numerator is not None:
            return numerator / denominator, "absolute"
    match = re.search(r"\d+(?:\.\d+)?", body)
    if match:
        value = float(match.group())
        rest = body[match.end():]
        if kind == "absolute":
            value *= _scale_after(rest)
        return value, kind
    cn = re.search(r"[零〇一二三四五六七八九十百千万亿两]+", body)
    if cn is None:
        return None
    parsed = _cn_to_float(cn.group())
    if parsed is None:
        return None
    return parsed, kind


def _scale_after(rest: str) -> float:
    """Use only the scale word that begins the text after the number."""
    lowered = rest.lstrip().lower()
    for token, factor in _QUANTITY_SCALE:
        if lowered.startswith(token.lower()):
            return float(factor)
    if re.match(r"b(?![a-z])", lowered):
        return 1e9
    if re.match(r"m(?![a-z])", lowered):
        return 1e6
    if re.match(r"k(?![a-z])", lowered):
        return 1e3
    return 1.0


def _cn_to_float(token: str) -> float | None:
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

    def section(part: str) -> float | None:
        if not part:
            return 0.0
        total = 0.0
        number = 0.0
        seen = False
        units = {"千": 1000.0, "百": 100.0, "十": 10.0}
        for char in part:
            if char in digits:
                number = digits[char]
                seen = True
                continue
            if char in units:
                total += (number or 1.0) * units[char]
                number = 0.0
                seen = True
                continue
            return None
        return total + number if seen else None

    if "亿" in token:
        left, right = token.split("亿", 1)
        left_value = _cn_to_float(left) if left else 1.0
        right_value = _cn_to_float(right) if right else 0.0
        if left_value is None or right_value is None:
            return None
        return left_value * 1e8 + right_value
    if "万" in token:
        left, right = token.split("万", 1)
        left_value = section(left) if left else 1.0
        right_value = section(right) if right else 0.0
        if left_value is None or right_value is None:
            return None
        return (left_value or 1.0) * 1e4 + right_value
    return section(token)


def _quantities_in(text: str) -> list[tuple[float, str]]:
    found: list[tuple[float, str]] = []
    for number in _unit_numbers(text):
        window = _context_around(text, number)
        parsed = _parse_quantity(window) if window else _parse_quantity(number)
        if parsed is not None:
            found.append(parsed)
    return found


def _context_around(text: str, number: str) -> str:
    normalized = _norm_digits(text)
    target = _norm_digits(number)
    index = normalized.find(target)
    if index < 0:
        return ""
    return normalized[index:index + len(target) + 24]


def _shareable_numbers(text: str) -> set[str]:
    found: set[str] = set()
    for match in _NUMBER.finditer(text):
        number = match.group()
        if _arabic_is_exempt(text, match.start(), match.end(), number):
            continue
        found.add(number)
    return found


def _number_signatures(text: str, number: str) -> list[tuple[float, str]]:
    normalized = _norm_digits(text)
    target = _norm_digits(number)
    found: list[tuple[float, str]] = []
    start = 0
    while True:
        index = normalized.find(target, start)
        if index < 0:
            break
        after = index + len(target)
        before_ok = index == 0 or normalized[index - 1] not in "0123456789."
        after_ok = after >= len(normalized) or normalized[after] not in "0123456789"
        if before_ok and after_ok and not _arabic_is_exempt(normalized, index, after, target):
            parsed = _parse_quantity(normalized[index:index + len(target) + 24])
            if parsed is not None:
                found.append(parsed)
        start = after
    return found


def _same_number_quantity(left: str, right: str, number: str) -> bool:
    for left_value, left_kind in _number_signatures(left, number):
        for right_value, right_kind in _number_signatures(right, number):
            if left_kind == right_kind and _near(left_value, right_value):
                return True
    return False


def _excerpt_shares_claim(event: Mapping[str, Any], excerpt: str) -> bool:
    if _BOILERPLATE.search(excerpt):
        return False
    claim = _claim_text(event)
    normalized_claim = _norm_digits(claim)
    normalized_excerpt = _norm_digits(excerpt)
    claim_numbers = _shareable_numbers(normalized_claim)
    excerpt_numbers = _shareable_numbers(normalized_excerpt)
    if any(_same_number_quantity(claim, excerpt, number) for number in claim_numbers & excerpt_numbers):
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


def _clause_bounds(text: str, index: int) -> tuple[int, int]:
    left = 0
    right = len(text)
    for cursor, _char in enumerate(text):
        if cursor < index and _clause_boundary(text, cursor):
            left = cursor + 1
        if cursor > index and _clause_boundary(text, cursor):
            right = cursor
            break
    return left, right


def _mask_non_predictions(text: str) -> str:
    """Hide compound nouns and product names that contain a prediction word."""
    chars = list(text)
    def blank(start: int, stop: int) -> None:
        for index in range(start, stop):
            chars[index] = " "

    for phrase in _PREDICTION_COMPOUND:
        found = text.find(phrase)
        while found >= 0:
            blank(found, found + len(phrase))
            found = text.find(phrase, found + len(phrase))
    for match in re.finditer(r"[A-Za-z][A-Za-z0-9]*\s*计划", text):
        blank(match.end() - 2, match.end())
    for match in re.finditer(
        r"\btarget(?=\s+(?!is\b|are\b|was\b|were\b|of\b|to\b|for\b)[A-Za-z])",
        text,
        re.IGNORECASE,
    ):
        blank(match.start(), match.end())
    return "".join(chars)


def _number_clause_predicts(text: str, number: str) -> bool:
    normalized = _norm_digits(text)
    target = _norm_digits(number)
    index = normalized.find(target)
    if index < 0:
        return False
    left, right = _clause_bounds(text, index)
    clause = _mask_non_predictions(text)[left:right]
    for match in _PREDICTION_WORD.finditer(clause):
        if not _clause_negates(text, left + match.start()):
            return True
    return False


def _support_excerpts(event: Mapping[str, Any]) -> list[str]:
    excerpts: list[str] = []
    sources = event.get("sources") or []
    if not isinstance(sources, list):
        return excerpts
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        support = source.get("support")
        if not isinstance(support, Mapping):
            continue
        excerpt = support.get("excerpt")
        if isinstance(excerpt, str) and excerpt.strip():
            excerpts.append(excerpt)
    return excerpts


def _measured_uses_prediction_word(event: Mapping[str, Any], claim: Mapping[str, Any]) -> bool:
    value = claim.get("value")
    if not isinstance(value, str) or not value:
        return False
    numbers = _NUMBER.findall(_norm_digits(value)) or [value]
    for text in [*_reader_fields(event), *_support_excerpts(event)]:
        for number in numbers:
            if _number_clause_predicts(text, number):
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
