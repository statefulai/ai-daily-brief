"""Per-source fetch status for the edition pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable

from sources import NewsItem, SourceFetchError

FetchFn = Callable[[dict], Awaitable[list[NewsItem]]]


@dataclass
class SourceResult:
    id: str
    name: str
    status: str
    items: list[NewsItem] = field(default_factory=list)
    error: str | None = None

    def to_record(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "item_count": len(self.items),
            "error": self.error,
        }


async def run_source(source_id: str, name: str, fetcher: FetchFn, config: dict) -> SourceResult:
    if not config.get("enabled", True):
        return SourceResult(source_id, name, "skipped")
    try:
        items = await fetcher(config)
    except SourceFetchError as exc:
        return SourceResult(source_id, name, "failed", error=str(exc))
    except Exception as exc:  # noqa: BLE001 - source failures must stay visible
        return SourceResult(source_id, name, "failed", error=str(exc))
    if items:
        return SourceResult(source_id, name, "success", items=items)
    return SourceResult(source_id, name, "no_candidates")
