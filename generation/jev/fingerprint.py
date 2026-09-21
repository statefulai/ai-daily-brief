"""Candidate identity and reuse keys. Title/URL-only changes are not new work."""

from __future__ import annotations

import hashlib
import json
from typing import Any


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


def normalize_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().rstrip("/").lower()


def evidence_payload(item: dict[str, Any]) -> dict[str, Any]:
    """Evidence used for reuse. Title, wording of headlines, and URLs are excluded."""
    facts: list[str] = []
    seen: set[str] = set()

    def add_fact(raw: Any) -> None:
        text = normalize_text(raw)
        if text and text not in seen:
            seen.add(text)
            facts.append(text)

    for fact in item.get("facts") or []:
        add_fact(fact)
    add_fact(item.get("text") or item.get("summary") or "")

    prior_norm: list[Any] = []
    for row in item.get("prior_coverage") or []:
        if isinstance(row, str):
            text = normalize_text(row)
            if text:
                prior_norm.append(text)
        elif isinstance(row, dict):
            prior_facts = [normalize_text(item) for item in row.get("facts") or []]
            prior_norm.append(
                {
                    "edition_id": normalize_text(row.get("edition_id") or ""),
                    "title": normalize_text(row.get("title") or ""),
                    "facts": [item for item in prior_facts if item],
                    "urls": sorted(
                        url
                        for url in (normalize_url(item) for item in row.get("urls") or [])
                        if url
                    ),
                }
            )
    return {"facts": facts, "prior_coverage": prior_norm}


def event_identity(item: dict[str, Any]) -> str:
    """Same event across reports. Title/URL-only differences do not mint a new id."""
    for key in (item.get("event_key"), item.get("topic_key")):
        text = normalize_text(key)
        if text:
            return f"key:{text}"
    facts = evidence_payload(item)["facts"]
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
