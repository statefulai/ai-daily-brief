from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Sequence

from edition import BEIJING_TZ
from source_status import SourceResult

logger = logging.getLogger(__name__)

ALERT_AFTER_DAYS = 3
STORE_VERSION = 1
DAY_KIND = "source_health_day"
ALERT_KIND = "source_health_alert"


@dataclass(frozen=True)
class SourceDay:
    edition_id: str
    source_id: str
    status: str
    item_count: int
    error: str | None
    observed_at: str


@dataclass(frozen=True)
class Streak:
    since: str
    days: int


@dataclass(frozen=True)
class SourceAlert:
    alert_id: str
    source_id: str
    failing_since: str
    consecutive_days: int
    error: str
    text: str


def observe_source_health(
    edition_id: str,
    results: Sequence[SourceResult],
    *,
    persist: bool,
    root: Path | None = None,
    now: datetime | None = None,
) -> list[SourceAlert]:
    store = _Store(root or _default_root())
    observed_at = (now or datetime.now(BEIJING_TZ)).isoformat()
    emitted: list[SourceAlert] = []
    try:
        alerts = [alert for result in results if (alert := _alert_for(store, edition_id, result))]
        if not persist:
            return alerts
        store.write_days([
            SourceDay(edition_id, result.id, result.status, len(result.items), result.error, observed_at)
            for result in results
        ])
        for alert in alerts:
            store.write_alert(alert)
            emitted.append(alert)
    except OSError as exc:
        logger.warning("source health store unavailable at %s: %s", store.root, exc)
    return emitted


def _alert_for(store: _Store, edition_id: str, result: SourceResult) -> SourceAlert | None:
    if result.status != "failed":
        return None
    streak = store.streak(edition_id, result.id)
    if streak.days < ALERT_AFTER_DAYS:
        return None
    earlier = store.streak_alerts(result.id, streak)
    if earlier and _newest_error(earlier) == result.error:
        return None
    head = f"来源告警：{result.name}（{result.id}）已连续 {streak.days} 天抓取失败，"
    if earlier:
        text = head + f"原因有变化。之前：{earlier[-1]['error']}。现在：{result.error}"
    else:
        text = head + f"自 {streak.since} 起。原因：{result.error}"
    n = store.next_alert_number(edition_id, result.id)
    return SourceAlert(
        alert_id=f"{edition_id}:{result.id}:{n}",
        source_id=result.id,
        failing_since=streak.since,
        consecutive_days=streak.days,
        error=result.error,
        text=text,
    )


def _newest_error(alerts: list[dict]) -> str | None:
    if not alerts:
        return None
    return alerts[-1]["error"]


def _default_root() -> Path:
    private = os.environ.get("AI_DAILY_PRIVATE_RUN_DIR", "").strip()
    return (Path(private) if private else Path.cwd() / "runs") / "source_health"


def _shift(edition_id: str, days: int) -> str:
    return (date.fromisoformat(edition_id) + timedelta(days=days)).isoformat()


class _Store:
    def __init__(self, root: Path):
        self.root = root

    def streak(self, edition_id: str, source_id: str) -> Streak:
        since = edition_id
        prior = _shift(edition_id, -1)
        while self._failed_on(prior, source_id):
            since = prior
            prior = _shift(prior, -1)
        days = (date.fromisoformat(edition_id) - date.fromisoformat(since)).days + 1
        return Streak(since, days)

    def streak_alerts(self, source_id: str, streak: Streak) -> list[dict]:
        return [
            alert
            for offset in range(streak.days)
            for _, alert in self._alerts_on(_shift(streak.since, offset), source_id)
            if alert and alert.get("failing_since") == streak.since
        ]

    def next_alert_number(self, edition_id: str, source_id: str) -> int:
        return max((n for n, _ in self._alerts_on(edition_id, source_id)), default=0) + 1

    def write_days(self, days: list[SourceDay]) -> None:
        for day in days:
            self.write_day(day)

    def write_day(self, day: SourceDay) -> None:
        payload = {"version": STORE_VERSION, "kind": DAY_KIND, **asdict(day)}
        _write_json(self._day_path(day.edition_id, day.source_id), payload)

    def write_alert(self, alert: SourceAlert) -> None:
        edition_id = alert.alert_id.split(":", 1)[0]
        n = alert.alert_id.rsplit(":", 1)[1]
        payload = {"version": STORE_VERSION, "kind": ALERT_KIND, **asdict(alert)}
        _write_json(self.root / "alerts" / edition_id / f"{alert.source_id}.{n}.json", payload)

    def _day_path(self, edition_id: str, source_id: str) -> Path:
        return self.root / "days" / edition_id / f"{source_id}.json"

    def _failed_on(self, edition_id: str, source_id: str) -> bool:
        path = self._day_path(edition_id, source_id)
        if not path.is_file():
            return False
        day = self._load(path, DAY_KIND)
        if day is None:
            return True
        return day.get("status") == "failed"

    def _alerts_on(self, edition_id: str, source_id: str) -> list[tuple[int, dict | None]]:
        directory = self.root / "alerts" / edition_id
        if not directory.is_dir():
            return []
        name = re.compile(rf"{re.escape(source_id)}\.(\d+)\.json")
        found = []
        for path in directory.iterdir():
            match = name.fullmatch(path.name)
            if match:
                found.append((int(match.group(1)), self._load(path, ALERT_KIND)))
        return sorted(found, key=lambda entry: entry[0])

    def _load(self, path: Path, kind: str) -> dict | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except ValueError:
            payload = None
        if isinstance(payload, dict) and payload.get("kind") == kind and payload.get("version") == STORE_VERSION:
            return payload
        logger.warning("ignoring malformed source health file %s", path)
        return None


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
