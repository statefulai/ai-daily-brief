"""Per-source fetch status for the edition pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

from sources import FETCHERS, NewsItem

FetchFn = Callable[[dict], Awaitable[list[NewsItem]]]


class SourceConfigError(ValueError):
    """config.yaml `sources:` is malformed. Raised before any fetch."""


@dataclass(frozen=True)
class SourceSpec:
    id: str
    name: str
    fetch: FetchFn
    params: Mapping[str, Any]
    disabled: str | None


def parse_source_specs(sources_cfg: Mapping[str, Any]) -> list[SourceSpec]:
    """The only reader of config `sources:`. Each key is a source id, and YAML order is kept.

    `disabled: <reason>` is the only off switch, so an `enabled` key is rejected.
    Disabled entries are checked in full, so restoring one cannot surface a typo later.
    """
    if not isinstance(sources_cfg, Mapping):
        raise SourceConfigError("sources must be a mapping of source id to entry")
    specs = []
    for source_id, entry in sources_cfg.items():
        label = f"sources.{source_id}"
        if not isinstance(entry, Mapping):
            raise SourceConfigError(f"{label} must be a mapping")
        if "enabled" in entry:
            raise SourceConfigError(f"{label}: use `disabled: <reason>` instead of `enabled`")
        kind = entry.get("kind")
        if kind not in FETCHERS:
            raise SourceConfigError(f"{label}.kind must be one of {sorted(FETCHERS)}, got {kind!r}")
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise SourceConfigError(f"{label}.name must be a non-empty string")
        disabled = entry.get("disabled")
        if disabled is not None and (not isinstance(disabled, str) or not disabled.strip()):
            raise SourceConfigError(f"{label}.disabled must be a non-empty reason")
        specs.append(SourceSpec(
            id=source_id,
            name=name,
            fetch=FETCHERS[kind],
            params={key: value for key, value in entry.items() if key not in ("kind", "disabled")},
            disabled=disabled,
        ))
    return specs


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


async def run_source(spec: SourceSpec) -> SourceResult:
    if spec.disabled:
        return SourceResult(spec.id, spec.name, "skipped")
    try:
        items = await spec.fetch(dict(spec.params))
    except Exception as exc:  # noqa: BLE001 - source failures must stay visible
        # str(httpx.ReadTimeout("")) is "", and a failed edition record needs an error.
        return SourceResult(spec.id, spec.name, "failed", error=str(exc) or type(exc).__name__)
    if items:
        return SourceResult(spec.id, spec.name, "success", items=items)
    return SourceResult(spec.id, spec.name, "no_candidates")
