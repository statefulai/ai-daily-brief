"""Candidate identity and reuse keys. Title/URL-only changes are not new work."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

# Split summary sentences for exact membership against structured facts.
# This is not a novelty heuristic: leftover claims always enter evidence.
_CLAIM_SPLIT = re.compile(r"[.!?。！？;；\n]+")
_TRAILING_PUNCT = ".!?。！？;；"


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def sha256_json(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value)).hexdigest()


def normalize_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.strip().lower().split())


def normalize_fact(value: Any) -> str:
    """Lowercase, collapse whitespace, strip trailing sentence punctuation."""
    return normalize_text(value).rstrip(_TRAILING_PUNCT)


def normalize_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().rstrip("/").lower()


def _summary_claims(text: Any) -> list[str]:
    if not isinstance(text, str):
        return []
    return [part for part in (normalize_fact(piece) for piece in _CLAIM_SPLIT.split(text)) if part]


def stable_facts(item: dict[str, Any]) -> list[str]:
    """Normalized, sorted, unique structured facts. No summary inference."""
    seen: set[str] = set()
    facts: list[str] = []
    for raw in item.get("facts") or []:
        text = normalize_fact(raw)
        if text and text not in seen:
            seen.add(text)
            facts.append(text)
    facts.sort()
    return facts


def unconfirmed_summary_claims(item: dict[str, Any]) -> list[str]:
    """Summary claims that are not already exact structured facts.

    Generate CA owns complete facts. A leftover claim means equivalence
    cannot be confirmed, so the claim must enter scoring evidence and an
    old score cannot be reused.
    """
    covered = set(stable_facts(item))
    extra: list[str] = []
    seen: set[str] = set()
    summary = item.get("text") or item.get("summary") or ""
    for claim in _summary_claims(summary):
        if claim in covered or claim in seen:
            continue
        seen.add(claim)
        extra.append(claim)
    extra.sort()
    return extra


def scoring_facts(item: dict[str, Any]) -> list[str]:
    """Fact evidence shared by reuse fingerprint and the Jev payload."""
    seen: set[str] = set()
    facts: list[str] = []
    for text in (*stable_facts(item), *unconfirmed_summary_claims(item)):
        if text not in seen:
            seen.add(text)
            facts.append(text)
    facts.sort()
    return facts


def evidence_payload(item: dict[str, Any]) -> dict[str, Any]:
    """Evidence used for reuse and scoring. Title and URLs are excluded."""
    prior_norm: list[Any] = []
    for row in item.get("prior_coverage") or []:
        if isinstance(row, str):
            text = normalize_text(row)
            if text:
                prior_norm.append(text)
        elif isinstance(row, dict):
            prior_facts = sorted(
                {normalize_fact(raw) for raw in row.get("facts") or [] if normalize_fact(raw)}
            )
            prior_norm.append(
                {
                    "edition_id": normalize_text(row.get("edition_id") or ""),
                    "title": normalize_text(row.get("title") or ""),
                    "facts": prior_facts,
                    "urls": sorted(
                        url
                        for url in (normalize_url(raw) for raw in row.get("urls") or [])
                        if url
                    ),
                }
            )
    return {"facts": scoring_facts(item), "prior_coverage": prior_norm}


def event_identity(item: dict[str, Any]) -> str:
    """Same event across reports. Title/URL-only differences do not mint a new id."""
    for key in (item.get("event_key"), item.get("topic_key")):
        text = normalize_text(key)
        if text:
            return f"key:{text}"
    facts = stable_facts(item)
    if facts:
        return sha256_json({"facts": facts})
    url = normalize_url(item.get("source_url") or item.get("url") or "")
    if url:
        return f"url:{url}"
    ident = normalize_text(item.get("id") or "")
    return f"id:{ident or 'unknown'}"


def rubric_id(questions: dict[str, Any]) -> str:
    return sha256_json(questions)


def reuse_fingerprint(item: dict[str, Any], questions_id: str) -> str:
    return sha256_json(
        {
            "event": event_identity(item),
            "evidence": evidence_payload(item),
            "rubric": questions_id,
        }
    )
