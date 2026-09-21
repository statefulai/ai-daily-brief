"""Frozen Jev questions and request state. Rubric text must stay stable mid-run."""

from __future__ import annotations

from typing import Any

from generation.jev.fingerprint import evidence_payload

DEFAULT_MODEL = "jev-latest"
DEFAULT_AUDIENCE = (
    "Readers of an open-source Chinese AI daily brief: engineers and product people "
    "who want verified model/tool/infra changes with clear scope notes."
)

DEFAULT_NEW_FACT_INSTRUCTIONS = (
    "Compared ONLY to the prior public coverage provided in state.prior_coverage, "
    "does the candidate contain at least one clear NEW factual claim "
    "(not rephrasing, not rumor escalation, not a pure schedule teaser)?"
)
DEFAULT_NEW_FACT_CRITERIA = {
    "true": "At least one concrete new fact not already in prior_coverage",
    "false": "No clear new fact vs prior_coverage (duplicate, vague, or evidence-poor)",
}
DEFAULT_READER_VALUE_INSTRUCTIONS = (
    "For the daily AI brief target reader described in state.audience, "
    "how valuable is this candidate if the new-fact question is set aside?"
)
DEFAULT_READER_VALUE_CRITERIA = [
    "Negligible: noise, pure promo, or irrelevant to readers",
    "Low: minor / niche / weak evidence",
    "Medium: worth a skim for many readers",
    "High: clear action or understanding value for most target readers",
]


def build_questions(rubric: dict[str, Any] | None = None) -> dict[str, Any]:
    """Fixed question text from rubric; do not change mid-run."""
    rubric = rubric or {}
    nf = rubric.get("has_clear_new_fact", {})
    rv = rubric.get("reader_value", {})
    return {
        "has_clear_new_fact": {
            "type": "noul",
            "instructions": nf.get("instructions", DEFAULT_NEW_FACT_INSTRUCTIONS),
            "criteria": nf.get("criteria", DEFAULT_NEW_FACT_CRITERIA),
        },
        "reader_value": {
            "type": "score",
            "instructions": rv.get("instructions", DEFAULT_READER_VALUE_INSTRUCTIONS),
            "criteria": rv.get("criteria", DEFAULT_READER_VALUE_CRITERIA),
        },
    }


def build_state(item: dict[str, Any], rubric: dict[str, Any] | None = None) -> dict[str, Any]:
    rubric = rubric or {}
    evidence = evidence_payload(item)
    return {
        "audience": rubric.get("audience", DEFAULT_AUDIENCE),
        "candidate": {
            "id": item.get("id"),
            "title": item.get("title"),
            "published_at": item.get("published_at") or item.get("published"),
            "source_url": item.get("source_url") or item.get("url"),
            "source_lang": item.get("source_lang"),
            "text": " ".join(evidence["facts"]),
            "tags": item.get("tags") or [],
            "facts": list(evidence["facts"]),
        },
        "prior_coverage": evidence["prior_coverage"],
        "notes_for_judge": item.get("notes_for_judge") or "",
    }
