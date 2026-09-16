"""Private delivery state contract; recipients and credentials never enter the ledger."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import re
from typing import Iterator, Literal

from edition import EDITION_ID_RE, EditionError

Channel = Literal["group", "email"]
DeliveryStatus = Literal["sent", "failed", "unknown"]
CHANNELS = {"group", "email"}
STATUSES = {"sent", "failed", "unknown"}


@dataclass(frozen=True)
class DeliveryKey:
    edition_id: str
    content_hash: str
    channel: Channel
    recipient_scope: str

    def __post_init__(self) -> None:
        if not EDITION_ID_RE.fullmatch(self.edition_id):
            raise EditionError("invalid delivery edition_id")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.content_hash):
            raise EditionError("invalid delivery content_hash")
        if self.channel not in CHANNELS:
            raise EditionError("unsupported delivery channel")
        if not self.recipient_scope.strip() or "|" in self.recipient_scope:
            raise EditionError("recipient_scope must be a non-empty label")

    @property
    def token(self) -> str:
        return "|".join(
            (self.edition_id, self.content_hash, self.channel, self.recipient_scope)
        )


class DeliveryLedger:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _read(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "records": {}}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if payload.get("version") != 1 or not isinstance(payload.get("records"), dict):
            raise EditionError("invalid delivery ledger")
        return payload

    @contextmanager
    def _write_lock(self) -> Iterator[None]:
        """Serialize the complete read-modify-write cycle across processes."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = Path(str(self.path) + ".lock")
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def status(self, key: DeliveryKey) -> DeliveryStatus | None:
        record = self._read()["records"].get(key.token)
        return record.get("status") if record else None

    def may_attempt(self, key: DeliveryKey) -> bool:
        return self.status(key) in {None, "failed"}

    def record(
        self,
        key: DeliveryKey,
        status: DeliveryStatus,
        *,
        detail: str | None = None,
    ) -> None:
        if status not in STATUSES:
            raise EditionError("unsupported delivery status")
        with self._write_lock():
            payload = self._read()
            current = payload["records"].get(key.token)
            if current and current.get("status") == "sent" and status != "sent":
                raise EditionError("sent delivery status cannot be downgraded")
            payload["records"][key.token] = {
                "key": asdict(key),
                "status": status,
                "detail": detail,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(self.path)
