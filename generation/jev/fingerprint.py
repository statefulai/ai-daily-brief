"""Candidate identity and reuse keys. Title/URL-only changes are not new work."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

_CLAIM_SPLIT = re.compile(r"[.!?。！？;；\n]+")
_LATIN_TOKEN = re.compile(r"[a-z0-9]+")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")

# Novelty check only. Folded fact strings stay as normalized source text.
_STOPWORDS = {
    "a",
    "an",
    "the",
    "and",
    "or",
    "of",
    "to",
    "for",
    "in",
    "on",
    "at",
    "by",
    "with",
    "from",
    "that",
    "this",
    "it",
    "is",
    "are",
    "was",
    "were",
    "be",
    "as",
    "its",
    "their",
    "has",
    "have",
    "had",
    "will",
    "would",
    "can",
    "today",
    "yesterday",
    "official",
    "officially",
    "company",
    "also",
    "brief",
    "note",
    "update",
    "news",
    "report",
    "according",
    "says",
    "said",
    "的",
    "了",
    "在",
    "与",
    "和",
    "及",
    "并",
    "将",
    "对",
    "为",
    "是",
    "已",
    "现",
    "会",
    "把",
    "被",
    "等",
    "其",
    "该",
    "本",
    "今日",
    "今天",
    "官方",
    "公司",
    "同时",
    "还",
}


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


def _content_tokens(text: str) -> set[str]:
    lowered = normalize_text(text)
    for stop in sorted(_STOPWORDS, key=len, reverse=True):
        if stop:
            lowered = lowered.replace(stop, " ")
    lowered = normalize_text(lowered)
    tokens: set[str] = set()
    for word in _LATIN_TOKEN.findall(lowered):
        if len(word) > 1:
            tokens.add(word)
    for run in _CJK_RUN.findall(lowered):
        if len(run) == 1:
            tokens.add(run)
        else:
            tokens.update(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


def _split_claims(text: Any) -> list[str]:
    if not isinstance(text, str):
        return []
    return [part for part in (normalize_text(piece) for piece in _CLAIM_SPLIT.split(text)) if part]


def _has_significant_token(tokens: set[str]) -> bool:
    return any(token.isdigit() or any(char.isdigit() for char in token) for token in tokens)


def is_material_claim(claim: str, existing_facts: list[str]) -> bool:
    """True for a new or corrected fact; false for wording-only restatement."""
    claim_tokens = _content_tokens(claim)
    if not claim_tokens:
        return False
    covered: set[str] = set()
    for fact in existing_facts:
        covered |= _content_tokens(fact)
    if not covered:
        return True
    novel = claim_tokens - covered
    if not novel:
        return False
    if _has_significant_token(novel):
        return True
    return len(novel) >= 2 and (len(novel) / len(claim_tokens)) >= 0.4


def stable_facts(item: dict[str, Any]) -> list[str]:
    """What's-new facts: declared facts plus material summary claims, sorted.

    Summary wording is not hashed. If the summary adds or corrects a fact,
    that claim is folded into this list first so reuse cannot skip it.
    """
    facts: list[str] = []
    seen: set[str] = set()

    def add(raw: Any) -> None:
        text = normalize_text(raw)
        if text and text not in seen:
            seen.add(text)
            facts.append(text)

    for fact in item.get("facts") or []:
        add(fact)

    summary = item.get("text") or item.get("summary") or ""
    for claim in _split_claims(summary):
        if claim in seen:
            continue
        if is_material_claim(claim, facts):
            add(claim)

    facts.sort()
    return facts


def evidence_payload(item: dict[str, Any]) -> dict[str, Any]:
    """Evidence used for reuse. Title, wording of headlines, and URLs are excluded."""
    prior_norm: list[Any] = []
    for row in item.get("prior_coverage") or []:
        if isinstance(row, str):
            text = normalize_text(row)
            if text:
                prior_norm.append(text)
        elif isinstance(row, dict):
            prior_facts = sorted(
                {
                    normalize_text(raw)
                    for raw in row.get("facts") or []
                    if normalize_text(raw)
                }
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
    return {"facts": stable_facts(item), "prior_coverage": prior_norm}


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
