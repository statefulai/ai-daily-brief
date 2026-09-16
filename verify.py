"""Primary-source verification from public page metadata."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import re
from typing import Any

import httpx

from edition import EditionError

DATE_PATTERNS = (
    re.compile(r'"datePublished"\s*:\s*"([^"]+)"'),
    re.compile(r'"dateCreated"\s*:\s*"([^"]+)"'),
    re.compile(r'property=["\']article:published_time["\']\s+content=["\']([^"\']+)["\']'),
    re.compile(r'property=["\']og:updated_time["\']\s+content=["\']([^"\']+)["\']'),
    re.compile(r'<meta[^>]+name=["\']date["\'][^>]+content=["\']([^"\']+)["\']', re.IGNORECASE),
)


def extract_published_at(html: str) -> str | None:
    if not isinstance(html, str) or not html.strip():
        return None
    for pattern in DATE_PATTERNS:
        match = pattern.search(html)
        if match:
            return match.group(1)
    return None


def verify_primary_source(url: str, html: str | None, *, fetch_error: str | None = None) -> dict[str, Any]:
    if not url or not url.startswith(("http://", "https://")):
        raise EditionError("primary source URL must be http(s)")
    if fetch_error:
        return {
            "url": url,
            "verified": False,
            "published_at": None,
            "reason": fetch_error,
        }
    if html is None:
        return {
            "url": url,
            "verified": False,
            "published_at": None,
            "reason": "primary source body was not fetched",
        }
    published_at = extract_published_at(html)
    if not published_at:
        return {
            "url": url,
            "verified": False,
            "published_at": None,
            "reason": "no published time in primary source metadata",
        }
    try:
        parsed = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError:
        return {
            "url": url,
            "verified": False,
            "published_at": None,
            "reason": "invalid published time in primary source metadata",
        }
    if parsed.tzinfo is None:
        return {
            "url": url,
            "verified": False,
            "published_at": None,
            "reason": "published time in primary source metadata has no timezone",
        }
    return {
        "url": url,
        "verified": True,
        "published_at": published_at,
        "reason": None,
    }


async def _fetch_primary_source(client: httpx.AsyncClient, url: str) -> dict[str, Any]:
    try:
        response = await client.get(
            url,
            headers={"User-Agent": "ai-daily-brief/1.0 (+source verification)"},
        )
    except Exception as exc:  # noqa: BLE001 - the failure reason is edition evidence
        return verify_primary_source(url, None, fetch_error=f"{type(exc).__name__}: {exc}")
    if response.status_code >= 400:
        return verify_primary_source(url, None, fetch_error=f"HTTP {response.status_code}")
    return verify_primary_source(url, response.text)


async def verify_event_primary_sources(
    events: list[dict],
    *,
    client: httpx.AsyncClient | None = None,
) -> list[dict]:
    """Fetch unverified primary pages and write the observed result into event sources."""
    verified_events = deepcopy(events)
    targets = [
        source
        for event in verified_events
        for source in event.get("sources", [])
        if source.get("kind") == "primary" and not source.get("verified", False)
    ]
    if not targets:
        return verified_events

    own_client = client is None
    active_client = client or httpx.AsyncClient(timeout=20, follow_redirects=True)
    cache: dict[str, dict[str, Any]] = {}
    try:
        for source in targets:
            url = source.get("url", "")
            result = cache.get(url)
            if result is None:
                result = await _fetch_primary_source(active_client, url)
                cache[url] = result
            source["verified"] = result["verified"]
            if result["published_at"]:
                source["published_at"] = result["published_at"]
            if result["reason"]:
                source["note"] = result["reason"]
    finally:
        if own_client:
            await active_client.aclose()
    return verified_events
