"""Merge same-event reports and check fields required for a Jev evaluation."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from generation.jev.fingerprint import (
    event_identity,
    evidence_payload,
    normalize_url,
    stable_facts,
)


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


def _structured_fact_strings(*items: dict[str, Any]) -> list[str]:
    rows: list[str] = []
    for item in items:
        for raw in item.get("facts") or []:
            if isinstance(raw, str):
                rows.append(raw)
    return rows


def _merge_into(current: dict[str, Any], incoming: dict[str, Any]) -> None:
    merged_facts = stable_facts({"facts": _structured_fact_strings(current, incoming)})
    if merged_facts:
        current["facts"] = merged_facts

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
        structured = stable_facts(item)
        if structured:
            item["facts"] = structured
        if key not in groups:
            groups[key] = item
            order.append(key)
            continue
        _merge_into(groups[key], item)
    return [groups[key] for key in order] + leftover
