"""Strict edition schema, content hash, mutex, and generation status."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

BEIJING_TZ = timezone(timedelta(hours=8))
EDITION_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
EDITION_STATUSES = ("published_candidate", "no_new_value", "failed")
SOURCE_STATUSES = ("success", "no_candidates", "failed", "skipped")
EVENT_WINDOWS = ("in_window", "recent")
EVENT_PLACEMENTS = ("lead", "story", "desk")
SCHEMA_VERSION = "1"
HASH_EXCLUDED_FIELDS = ("content_hash", "generated_at", "attempt")
READER_LEDE_GUIDE = 120
READER_LEDE_MAX = 200
READER_CAVEAT_GUIDE = 40
READER_EXCEPTION_MAX = 2
# A bare date (2026-10-09, 2026/10/09) or a Chinese date is not a timestamp.
# After a clock, only a glued uppercase Z counts, and it cannot be followed
# by a letter, digit, or CJK character. +HH:MM and +HHMM may have whitespace
# before the sign. +HH and -HHMM count only when glued. -HH:MM and -HH are
# ranges, not offsets. UTC/GMT count only when an offset follows.
_READER_CLOCK = r"\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
_READER_TZ = r"(?:Z|UTC|GMT|[+-]\d{2}:?\d{2})"
_READER_OFFSET_CLOCK = r"(?<!\d)\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
READER_TIME_METADATA_RE = re.compile(
    r"datepublished|datemodified|createdat|lastmod"
    rf"|\d{{4}}-\d{{2}}-\d{{2}}(?:[Tt]| +){_READER_CLOCK}(?:\s*{_READER_TZ})?"
    rf"|\d{{4}}/\d{{2}}/\d{{2}}(?:[Tt]| +){_READER_CLOCK}(?:\s*{_READER_TZ})?"
    rf"|(?<![A-Za-z0-9])[Tt]{_READER_CLOCK}(?:\s*{_READER_TZ})?"
    rf"|{_READER_OFFSET_CLOCK}\s*\+\d{{2}}:\d{{2}}(?!\d)"
    rf"|{_READER_OFFSET_CLOCK}\s*\+\d{{4}}(?!\d)"
    rf"|{_READER_OFFSET_CLOCK}-\d{{4}}(?!\d)"
    rf"|{_READER_OFFSET_CLOCK}\+\d{{2}}(?![\d:])"
    rf"|{_READER_OFFSET_CLOCK}(?-i:Z)(?![A-Za-z0-9\u4e00-\u9fff])"
    rf"|{_READER_OFFSET_CLOCK}\s*(?:UTC|GMT)\s*[+-]\d{{1,4}}(?::\d{{2}})?(?!\d)",
    re.IGNORECASE,
)
_READER_URL_RE = re.compile(r"https?://\S+")


class EditionError(ValueError):
    """Raised when an edition document violates the schema contract."""


def beijing_today(now: datetime | None = None) -> datetime:
    current = now.astimezone(BEIJING_TZ) if now else datetime.now(BEIJING_TZ)
    return current


def edition_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Return the latest closed Beijing-time daily window [start, cutoff)."""
    current = beijing_today(now)
    cutoff = current.replace(hour=8, minute=0, second=0, microsecond=0)
    if current < cutoff:
        cutoff -= timedelta(days=1)
    return cutoff - timedelta(days=1), cutoff


def edition_id_for(now: datetime | None = None) -> str:
    _, cutoff = edition_window(now)
    return cutoff.strftime("%Y-%m-%d")


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise EditionError(f"{label} must be an object")
    return dict(value)


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EditionError(f"{label} must be a non-empty string")
    return value.strip()


def _require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise EditionError(f"{label} must be a list")
    return value


def _require_optional_text(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _require_text(value, label)


def visible_char_count(text: str) -> int:
    """Reader-facing length: drop links and whitespace; each remaining character counts as 1."""
    return len(re.sub(r"\s+", "", _READER_URL_RE.sub("", text)))


def reader_visible_text(text: str) -> str:
    return _READER_URL_RE.sub("", text)


def text_has_time_metadata(text: str) -> bool:
    return READER_TIME_METADATA_RE.search(reader_visible_text(text)) is not None


def strip_leading_time_metadata(text: str) -> str:
    """Drop leading sentences that leak source timestamps. Used only for display."""
    parts = re.split(r"(?<=[。！？])", text)
    kept: list[str] = []
    skipping = True
    for part in parts:
        if skipping and part.strip() and text_has_time_metadata(part):
            continue
        skipping = False
        kept.append(part)
    result = "".join(kept).strip()
    return result or text.strip()


def _reader_field(payload: Mapping[str, Any], key: str) -> str | None:
    if key not in payload or payload[key] is None:
        return None
    return _require_text(payload[key], f"event.{key}")


def _reject_reader_time_metadata(text: str, label: str) -> None:
    if text_has_time_metadata(text):
        raise EditionError(f"{label} must not include source time metadata")


def _check_reader_contract(events: list[dict[str, Any]]) -> None:
    """Hard limits for events that opt into lede/caveat. Legacy events are skipped."""
    exceptions = 0
    for event in events:
        lede = event.get("lede")
        caveat = event.get("caveat")
        reason = event.get("length_exception")
        if lede is None and caveat is None and reason is None:
            continue
        if not lede:
            raise EditionError("event.lede is required when caveat or length_exception is set")
        _reject_reader_time_metadata(lede, f"event.lede ({event['id']})")
        if caveat is not None:
            _reject_reader_time_metadata(caveat, f"event.caveat ({event['id']})")
        count = visible_char_count(lede)
        if count > READER_LEDE_MAX:
            raise EditionError(f"event.lede exceeds 200 characters ({count})")
        if count > READER_LEDE_GUIDE and not reason:
            raise EditionError(
                f"event.lede exceeds 120 characters without length_exception ({count})"
            )
        if reason:
            exceptions += 1
    if exceptions > READER_EXCEPTION_MAX:
        raise EditionError("edition may include at most 2 length_exception events")


def reader_contract_warnings(edition: Mapping[str, Any]) -> list[str]:
    """Soft limits. Callers may print these; they do not fail validation."""
    warnings: list[str] = []
    for event in edition.get("events") or []:
        if not isinstance(event, Mapping):
            continue
        lede = event.get("lede")
        caveat = event.get("caveat")
        reason = event.get("length_exception")
        event_id = event.get("id", "?")
        if isinstance(lede, str) and reason and visible_char_count(lede) > READER_LEDE_GUIDE:
            warnings.append(
                f"{event_id}: lede is {visible_char_count(lede)} characters with length_exception"
            )
        if isinstance(caveat, str) and visible_char_count(caveat) > READER_CAVEAT_GUIDE:
            warnings.append(f"{event_id}: caveat is {visible_char_count(caveat)} characters")
    return warnings


def _require_http_url(value: Any, label: str) -> str:
    url = _require_text(value, label)
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise EditionError(f"{label} must be an absolute http(s) URL")
    return url


def _require_datetime(value: Any, label: str) -> str:
    text = _require_text(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EditionError(f"{label} must be an ISO 8601 datetime") from exc
    if parsed.tzinfo is None:
        raise EditionError(f"{label} must include a timezone")
    return text


def validate_source_record(record: Mapping[str, Any]) -> dict[str, Any]:
    payload = _require_mapping(record, "source")
    status = _require_text(payload.get("status"), "source.status")
    if status not in SOURCE_STATUSES:
        raise EditionError(f"unsupported source status: {status}")
    item_count = payload.get("item_count", 0)
    if type(item_count) is not int or item_count < 0:
        raise EditionError("source.item_count must be a non-negative integer")
    error = payload.get("error")
    if error is not None and (not isinstance(error, str) or not error.strip()):
        raise EditionError("source.error must be a non-empty string when present")
    if status == "failed" and not error:
        raise EditionError("failed source must include error")
    if status == "success" and item_count == 0:
        raise EditionError("successful source must include at least one item")
    if status == "no_candidates" and item_count != 0:
        raise EditionError("no_candidates source must have item_count 0")
    return {
        "id": _require_text(payload.get("id"), "source.id"),
        "status": status,
        "item_count": item_count,
        "error": error.strip() if isinstance(error, str) else None,
    }


def validate_event_source(record: Mapping[str, Any]) -> dict[str, Any]:
    payload = _require_mapping(record, "event.source")
    kind = payload.get("kind", "primary")
    if kind not in {"primary", "secondary"}:
        raise EditionError("event.source.kind must be primary or secondary")
    verified = payload.get("verified")
    if type(verified) is not bool:
        raise EditionError("event.source.verified must be a boolean")
    published_at = _require_optional_text(payload.get("published_at"), "event.source.published_at")
    if published_at is not None:
        _require_datetime(published_at, "event.source.published_at")
    return {
        "label": _require_text(payload.get("label"), "event.source.label"),
        "url": _require_http_url(payload.get("url"), "event.source.url"),
        "kind": kind,
        "published_at": published_at,
        "verified": verified,
        "note": _require_optional_text(payload.get("note"), "event.source.note"),
    }


def validate_event(record: Mapping[str, Any]) -> dict[str, Any]:
    payload = _require_mapping(record, "event")
    placement = _require_text(payload.get("placement"), "event.placement")
    window = _require_text(payload.get("window"), "event.window")
    if placement not in EVENT_PLACEMENTS:
        raise EditionError(f"unsupported event placement: {placement}")
    if window not in EVENT_WINDOWS:
        raise EditionError(f"unsupported event window: {window}")
    facts = [_require_text(item, "event.fact") for item in _require_list(payload.get("facts"), "event.facts")]
    conditions = [
        _require_text(item, "event.condition")
        for item in _require_list(payload.get("conditions", []), "event.conditions")
    ]
    background = [
        _require_text(item, "event.background")
        for item in _require_list(payload.get("background", []), "event.background")
    ]
    sources = [
        validate_event_source(item)
        for item in _require_list(payload.get("sources"), "event.sources")
    ]
    if not facts:
        raise EditionError("event.facts must not be empty")
    if not sources:
        raise EditionError("event.sources must not be empty")
    if not any(source["kind"] == "primary" for source in sources):
        raise EditionError("event must include a primary source")
    event = {
        "id": _require_text(payload.get("id"), "event.id"),
        "placement": placement,
        "window": window,
        "title": _require_text(payload.get("title"), "event.title"),
        "kicker": _require_text(payload.get("kicker"), "event.kicker"),
        "facts": facts,
        "conditions": conditions,
        "background": background,
        "sources": sources,
    }
    lede = _reader_field(payload, "lede")
    caveat = _reader_field(payload, "caveat")
    length_exception = _reader_field(payload, "length_exception")
    if lede is not None:
        event["lede"] = lede
    if caveat is not None:
        event["caveat"] = caveat
    if length_exception is not None:
        event["length_exception"] = length_exception
    return event


def canonical_payload(edition: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(edition)
    for field in HASH_EXCLUDED_FIELDS:
        payload.pop(field, None)
    return payload


def content_hash(edition: Mapping[str, Any]) -> str:
    encoded = json.dumps(canonical_payload(edition), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def validate_edition(document: Mapping[str, Any]) -> dict[str, Any]:
    payload = _require_mapping(document, "edition")
    edition_id = _require_text(payload.get("edition_id"), "edition_id")
    if not EDITION_ID_RE.fullmatch(edition_id):
        raise EditionError(f"invalid edition_id: {edition_id}")
    status = _require_text(payload.get("status"), "status")
    if status not in EDITION_STATUSES:
        raise EditionError(f"unsupported edition status: {status}")
    attempt = payload.get("attempt", 1)
    if type(attempt) is not int or attempt < 1:
        raise EditionError("attempt must be an integer >= 1")
    sources = [validate_source_record(item) for item in _require_list(payload.get("sources"), "sources")]
    events = [validate_event(item) for item in _require_list(payload.get("events", []), "events")]
    _check_reader_contract(events)
    source_ids = [item["id"] for item in sources]
    event_ids = [item["id"] for item in events]
    if len(source_ids) != len(set(source_ids)):
        raise EditionError("source ids must be unique")
    if len(event_ids) != len(set(event_ids)):
        raise EditionError("event ids must be unique")
    if sum(item["placement"] == "lead" for item in events) > 1:
        raise EditionError("edition may include at most one lead event")
    if status == "published_candidate" and not events:
        raise EditionError("published_candidate must include events")
    if status == "no_new_value" and events:
        raise EditionError("no_new_value must not include events")
    if status == "no_new_value" and any(item["status"] == "failed" for item in sources):
        raise EditionError("source failure cannot be recorded as no_new_value")
    if status == "failed":
        if events:
            raise EditionError("failed edition must not include events")
        failure = _require_mapping(payload.get("failure"), "failure")
        _require_text(failure.get("stage"), "failure.stage")
        _require_text(failure.get("message"), "failure.message")
    elif payload.get("failure") not in (None, {}):
        raise EditionError("only failed editions may include failure")

    edition = {
        "schema_version": _require_text(payload.get("schema_version"), "schema_version"),
        "edition_id": edition_id,
        "timezone": _require_text(payload.get("timezone"), "timezone"),
        "cutoff_at": _require_datetime(payload.get("cutoff_at"), "cutoff_at"),
        "generated_at": (
            _require_datetime(payload.get("generated_at"), "generated_at")
            if payload.get("generated_at") is not None
            else None
        ),
        "attempt": attempt,
        "status": status,
        "sources": sources,
        "events": events,
        "contribution_count": payload.get("contribution_count", 0),
        "failure": payload.get("failure") if status == "failed" else None,
    }
    if edition["schema_version"] != SCHEMA_VERSION:
        raise EditionError(f"unsupported schema_version: {edition['schema_version']}")
    if edition["timezone"] != "Asia/Shanghai":
        raise EditionError("timezone must be Asia/Shanghai")
    if type(edition["contribution_count"]) is not int or edition["contribution_count"] < 0:
        raise EditionError("contribution_count must be a non-negative integer")
    digest = content_hash(edition)
    existing = payload.get("content_hash")
    if existing and existing != digest:
        raise EditionError("content_hash does not match canonical payload")
    edition["content_hash"] = digest
    return edition


def decide_generation_status(
    source_records: Iterable[Mapping[str, Any]],
    events: Iterable[Mapping[str, Any]],
    *,
    generation_error: str | None = None,
) -> str:
    records = [validate_source_record(item) for item in source_records]
    event_list = list(events)
    if generation_error:
        return "failed"
    if any(item["status"] == "failed" for item in records) and not event_list:
        return "failed"
    if event_list:
        return "published_candidate"
    return "no_new_value"


def next_attempt(editions_dir: Path, edition_id: str) -> int:
    path = Path(editions_dir) / edition_id / "edition.json"
    if not path.exists():
        return 1
    current = json.loads(path.read_text(encoding="utf-8"))
    attempt = current.get("attempt", 1)
    if type(attempt) is not int or attempt < 1:
        raise EditionError("existing edition attempt is invalid")
    if current.get("edition_id") != edition_id:
        raise EditionError("existing edition_id does not match lock target")
    return attempt + 1


def acquire_edition_lock(editions_dir: Path, edition_id: str) -> Path:
    if not EDITION_ID_RE.fullmatch(edition_id):
        raise EditionError(f"invalid edition_id: {edition_id}")
    lock_dir = Path(editions_dir) / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{edition_id}.lock"
    try:
        with lock_path.open("x", encoding="utf-8") as lock_file:
            lock_file.write(edition_id)
    except FileExistsError:
        raise EditionError(f"edition {edition_id} is already running")
    return lock_path


def release_edition_lock(lock_path: Path) -> None:
    lock_path.unlink(missing_ok=True)


def build_edition(
    *,
    edition_id: str,
    cutoff_at: str,
    sources: Iterable[Mapping[str, Any]],
    events: Iterable[Mapping[str, Any]],
    attempt: int = 1,
    contribution_count: int = 0,
    generated_at: str | None = None,
    generation_error: str | None = None,
) -> dict[str, Any]:
    event_list = [validate_event(item) for item in events]
    source_list = [validate_source_record(item) for item in sources]
    status = decide_generation_status(source_list, event_list, generation_error=generation_error)
    document = {
        "schema_version": SCHEMA_VERSION,
        "edition_id": edition_id,
        "timezone": "Asia/Shanghai",
        "cutoff_at": cutoff_at,
        "generated_at": generated_at or beijing_today().isoformat(),
        "attempt": attempt,
        "status": status,
        "sources": source_list,
        "events": event_list if status == "published_candidate" else [],
        "contribution_count": contribution_count,
        "failure": (
            {"stage": "generation", "message": generation_error}
            if status == "failed"
            else None
        ),
    }
    if status == "failed" and not generation_error:
        document["failure"] = {
            "stage": "sources",
            "message": "one or more sources failed and no publishable events remain",
        }
    return validate_edition(document)


def write_edition(editions_dir: Path, document: Mapping[str, Any]) -> Path:
    edition = validate_edition(document)
    if edition["status"] != "published_candidate":
        raise EditionError("only published_candidate editions may be written")
    target_dir = Path(editions_dir) / edition["edition_id"]
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / "edition.json"
    path.write_text(json.dumps(edition, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def merge_events(events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for raw in events:
        event = validate_event(raw)
        primary = next(source["url"].rstrip("/").lower() for source in event["sources"] if source["kind"] == "primary")
        if primary not in merged:
            merged[primary] = event
            order.append(primary)
            continue
        current = merged[primary]
        seen = {source["url"].rstrip("/").lower() for source in current["sources"]}
        for source in event["sources"]:
            key = source["url"].rstrip("/").lower()
            if key not in seen:
                current["sources"].append(source)
                seen.add(key)
        for fact in event["facts"]:
            if fact not in current["facts"]:
                current["facts"].append(fact)
    return [merged[key] for key in order]
