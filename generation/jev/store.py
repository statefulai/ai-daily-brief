"""Durable Jev quota and reuse state in private run storage (not editions/ledgers)."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
import fcntl
import json
import os

from edition import BEIJING_TZ, beijing_today

STORE_VERSION = 1
STORE_KIND = "jev_assist"
DEFAULT_LIMIT = 100


class StoreError(RuntimeError):
    """Private run state cannot be restored safely; Jev must be skipped."""


def beijing_calendar_date(now: datetime | None = None) -> str:
    return beijing_today(now).strftime("%Y-%m-%d")


def resolve_store_path(
    *,
    now: datetime | None = None,
    environ: dict[str, str] | None = None,
    explicit: str | Path | None = None,
    repo_root: str | Path | None = None,
) -> Path:
    """Resolve the dated quota/reuse file inside private run storage."""
    env = environ if environ is not None else os.environ
    if explicit:
        return Path(explicit)
    store_file = (env.get("JEV_ASSIST_STORE") or "").strip()
    if store_file:
        return Path(store_file)
    date = beijing_calendar_date(now)
    filename = f"quota-{date}.json"
    root = (env.get("AI_DAILY_PRIVATE_RUN_DIR") or "").strip()
    if root:
        return Path(root) / "jev" / filename
    base = Path(repo_root) if repo_root is not None else Path.cwd()
    return base / "runs" / "jev" / filename


def empty_payload(calendar_date: str, request_limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    return {
        "version": STORE_VERSION,
        "kind": STORE_KIND,
        "timezone": "Asia/Shanghai",
        "calendar_date": calendar_date,
        "request_limit": request_limit,
        "request_count": 0,
        "evaluations": {},
    }


@dataclass
class Claim:
    kind: str  # reuse | claimed | quota_exhausted
    record: dict[str, Any] | None
    request_count: int


class JevRunStore:
    """fcntl-locked JSON store. One Beijing calendar day per file."""

    def __init__(
        self,
        path: Path,
        *,
        calendar_date: str,
        request_limit: int = DEFAULT_LIMIT,
    ):
        self.path = Path(path)
        self.calendar_date = calendar_date
        self.request_limit = request_limit
        self._ready = False

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = Path(str(self.path) + ".lock")
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _read_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            if self._ready:
                raise StoreError("private run store disappeared after restore")
            return empty_payload(self.calendar_date, self.request_limit)
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StoreError("private run store cannot be read") from exc
        if not isinstance(payload, dict):
            raise StoreError("private run store is not an object")
        if payload.get("version") != STORE_VERSION or payload.get("kind") != STORE_KIND:
            raise StoreError("private run store has an unsupported format")
        if payload.get("calendar_date") != self.calendar_date:
            raise StoreError("private run store calendar_date does not match")
        if payload.get("timezone") != "Asia/Shanghai":
            raise StoreError("private run store timezone must be Asia/Shanghai")
        evaluations = payload.get("evaluations")
        count = payload.get("request_count")
        if not isinstance(evaluations, dict) or type(count) is not int or count < 0:
            raise StoreError("private run store counters are invalid")
        return payload

    def _write_unlocked(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            temporary.write_text(encoded, encoding="utf-8")
            temporary.replace(self.path)
        except OSError as exc:
            raise StoreError("private run store cannot be written") from exc

    def restore(self) -> dict[str, Any]:
        """Load existing state and prove a read/write round-trip. Never reset on failure."""
        with self._lock():
            payload = self._read_unlocked()
            self._write_unlocked(payload)
            again = self._read_unlocked()
            if again.get("request_count") != payload.get("request_count"):
                raise StoreError("private run store round-trip failed")
            self._ready = True
            return again

    def snapshot(self) -> dict[str, Any]:
        with self._lock():
            return self._read_unlocked()

    def claim(self, fingerprint: str, *, now: datetime | None = None) -> Claim:
        claimed_at = (now or datetime.now(BEIJING_TZ)).isoformat()
        with self._lock():
            payload = self._read_unlocked()
            existing = payload["evaluations"].get(fingerprint)
            if isinstance(existing, dict):
                return Claim("reuse", existing, payload["request_count"])
            if payload["request_count"] >= self.request_limit:
                return Claim("quota_exhausted", None, payload["request_count"])
            payload["request_count"] += 1
            record = {
                "status": "in_flight",
                "consumed_quota": True,
                "claimed_at": claimed_at,
                "finished_at": None,
                "answers": None,
                "error": None,
                "model": None,
            }
            payload["evaluations"][fingerprint] = record
            self._write_unlocked(payload)
            return Claim("claimed", record, payload["request_count"])

    def complete(
        self,
        fingerprint: str,
        *,
        status: str,
        answers: dict[str, Any] | None = None,
        error: str | None = None,
        model: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        finished_at = (now or datetime.now(BEIJING_TZ)).isoformat()
        with self._lock():
            payload = self._read_unlocked()
            record = dict(payload["evaluations"].get(fingerprint) or {})
            record["status"] = status
            record["consumed_quota"] = True
            record["finished_at"] = finished_at
            record["answers"] = answers
            record["error"] = error
            record["model"] = model
            if "claimed_at" not in record:
                record["claimed_at"] = finished_at
            payload["evaluations"][fingerprint] = record
            self._write_unlocked(payload)
            return payload
