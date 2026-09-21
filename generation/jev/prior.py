"""Load prior public coverage from published editions (not from delivery ledgers)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def load_prior_coverage(
    editions_dir: str | Path | None,
    *,
    exclude_edition_id: str | None = None,
    limit: int = 48,
) -> list[dict[str, Any]]:
    if not editions_dir:
        return []
    root = Path(editions_dir)
    if not root.exists() or not root.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    try:
        paths = sorted(root.glob("*/edition.json"), reverse=True)
    except OSError:
        return []
    for path in paths:
        edition_id = path.parent.name
        if exclude_edition_id and edition_id == exclude_edition_id:
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.warning("Skipping unreadable edition %s: %s", path, type(exc).__name__)
            continue
        if not isinstance(document, dict):
            continue
        for event in document.get("events") or []:
            if not isinstance(event, dict):
                continue
            urls = [
                source.get("url")
                for source in event.get("sources") or []
                if isinstance(source, dict) and source.get("url")
            ]
            rows.append(
                {
                    "edition_id": document.get("edition_id") or edition_id,
                    "title": event.get("title"),
                    "facts": list(event.get("facts") or []),
                    "urls": urls,
                }
            )
            if len(rows) >= limit:
                return rows
    return rows
