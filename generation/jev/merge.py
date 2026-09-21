"""Merge same-event reports and check fields required for a Jev evaluation."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from generation.jev.fingerprint import event_identity, evidence_payload, normalize_url


def has_required_fields(item: dict[str, Any]) -> bool:
    """A scorable candidate needs an event identity and at least one evidence fact."""
    if not isinstance(item, dict):
        return False
    identity = event_identity(item)
    if identity in {"id:unknown", "id:"}:
        return False
    return bool(evidence_payload(item)["facts"])


def _source_rows(item: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for key in ("sources", "related_sources"):
        for raw in item.get(key) or []:
            if not isinstance(raw, dict):
                continue
            url = normalize_url(raw.get("url") or raw.get("source_url") or "")
            marker = url or str(raw)
            if marker in seen:
                continue
            seen.add(marker)
            rows.append(dict(raw))
    url = normalize_url(item.get("source_url") or item.get("url") or "")
    if url and url not in seen:
        rows.append(
            {
                "url": item.get("source_url") or item.get("url"),
                "source": item.get("source") or item.get("kicker"),
                "title": item.get("title"),
            }
        )
    return rows


def _merge_into(current: dict[str, Any], incoming: dict[str, Any]) -> None:
    current_facts = [fact for fact in current.get("facts") or [] if isinstance(fact, str)]
    for fact in incoming.get("facts") or []:
        if isinstance(fact, str) and fact not in current_facts:
            current_facts.append(fact)
    extra_text = incoming.get("text") or incoming.get("summary")
    if isinstance(extra_text, str) and extra_text.strip() and extra_text not in current_facts:
        current_summary = current.get("text") or current.get("summary") or ""
        if extra_text.strip() != (current_summary.strip() if isinstance(current_summary, str) else ""):
            current_facts.append(extra_text)
    if current_facts:
        current["facts"] = current_facts

    sources = _source_rows(current)
    seen = {normalize_url(row.get("url") or row.get("source_url") or "") for row in sources}
    for row in _source_rows(incoming):
        url = normalize_url(row.get("url") or row.get("source_url") or "")
        if url and url not in seen:
            sources.append(row)
            seen.add(url)
    if sources:
        current["sources"] = sources

    incoming_prior = incoming.get("prior_coverage") or []
    if incoming_prior and not current.get("prior_coverage"):
        current["prior_coverage"] = list(incoming_prior)


def merge_same_event(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse multiple reports of one event. Invalid rows stay, unscored."""
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    leftover: list[dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        item = deepcopy(raw)
        if not has_required_fields(item):
            leftover.append(item)
            continue
        key = event_identity(item)
        if key not in groups:
            groups[key] = item
            order.append(key)
            continue
        _merge_into(groups[key], item)
    return [groups[key] for key in order] + leftover
