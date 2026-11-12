"""Apply one approved editorial revision to a single edition workspace.

The helper removes events the policy rejected or the editor rejected, and it
applies wording only for events on the editorial rejection list. It does not
change ``source.kind``, collect stories, bump attempt, or mint an execution id.
Demoting a primary to secondary is not implemented.

A 40-digit head is a shape check. It does not prove the object exists in git
or that a platform returned it. ``execution_id`` stays empty.

The revision number is reserved only after this process holds ``revision.lock``.
The lock holder writes the attempted row, then ``applied`` or ``blocked``.
A caller that does not get the lock writes nothing. A closed number is not
reused. A dispatcher outside this repository may reserve the same number in
its own run record before calling ``apply``. This module does not read that
record.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import re
from pathlib import Path
from typing import Any, Iterator, Mapping

from candidate_policy import (
    MAX_REVISION,
    assess_new_candidate,
    evidence_failure,
    require_new_candidate,
)
from delivery.state import DeliveryLedger
from edition import EditionError, validate_edition
from outputs import render_web_edition

WORDING_FIELDS = {"title", "kicker", "lede", "caveat", "fact"}
_GIT_HEAD = re.compile(r"^[0-9a-f]{40}$")


class RevisionWorkspace:
    def __init__(self, root: Path):
        self.root = Path(root)

    @property
    def edition_path(self) -> Path:
        return self.root / "edition.json"

    @property
    def html_path(self) -> Path:
        return self.root / "index.html"

    @property
    def state_path(self) -> Path:
        return self.root / "revision-state.json"

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / "revision.lock"
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise EditionError("revision writer is already running") from exc
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def apply(
        self,
        contract: Mapping[str, Any],
        *,
        current_head: str,
        ledger: DeliveryLedger | None = None,
        observed_head: str | None = None,
    ) -> dict[str, Any]:
        """Apply one revision. ``ledger`` and ``observed_head`` are required.

        ``observed_head`` must be the pull request head read before merge, for
        example the ``head.sha`` in the GitHub event payload or ``git rev-parse``
        of the fetched PR ref. This module does not call GitHub. A matching
        string does not prove that the platform produced the commit.
        """
        parsed = _parse_contract(contract)
        with self._lock():
            state = self._read_state()
            key = _completion_key(parsed)
            existing = state["completions"].get(key)
            if existing:
                if (
                    existing.get("status") == "applied"
                    and existing.get("result")
                    and _same_contract(existing["contract"], parsed)
                ):
                    return existing["result"]
                if not (
                    existing.get("status") == "attempted" and _same_contract(existing["contract"], parsed)
                ):
                    raise EditionError("revision number is already reserved")
                self._remember_edition()
                try:
                    return self._finish(parsed, current_head, key, ledger, observed_head)
                except Exception:
                    self._restore_edition()
                    raise
            if self._revision_already_logged(parsed["revision"]):
                raise EditionError("revision number is already used")
            state["completions"][key] = {
                "contract": parsed,
                "status": "attempted",
                "reason": None,
                "result": None,
            }
            self._write_state(state)
            self._note_revision(parsed, "attempted", None)
            try:
                result = self._finish(parsed, current_head, key, ledger, observed_head)
            except Exception as exc:
                self._restore_edition()
                state = self._read_state()
                row = state["completions"][key]
                row["status"] = "blocked"
                row["reason"] = str(exc)
                row["result"] = None
                self._write_state(state)
                self._note_revision(parsed, "blocked", str(exc))
                raise
            return result

    def _finish(
        self,
        parsed: Mapping[str, Any],
        current_head: str,
        key: str,
        ledger: DeliveryLedger | None,
        observed_head: str | None,
    ) -> dict[str, Any]:
        self._remember_edition()
        result = self._perform(parsed, current_head, ledger, observed_head)
        state = self._read_state()
        state["completions"][key]["status"] = "applied"
        state["completions"][key]["reason"] = None
        state["completions"][key]["result"] = result
        self._write_state(state)
        self._note_revision(parsed, "applied", None)
        return result

    def _perform(
        self,
        parsed: Mapping[str, Any],
        current_head: str,
        ledger: DeliveryLedger | None,
        observed_head: str | None,
    ) -> dict[str, Any]:
        if not 1 <= parsed["revision"] <= MAX_REVISION:
            raise EditionError(f"revision is outside the bound of {MAX_REVISION}")
        base_head = parse_git_head(parsed["base_head"], "revision.base_head")
        current = parse_git_head(current_head, "revision.current_head")
        if current != base_head:
            raise EditionError("stale revision head")
        if ledger is None:
            raise EditionError("delivery ledger is required")
        if _edition_delivery_frozen(ledger, parsed["edition_id"]):
            raise EditionError("edition is frozen after delivery")
        if observed_head is None:
            raise EditionError("observed head is required")
        require_head_agreement(current, observed_head)
        editorial = _bind_editorial(parsed["editorial_rejections"], parsed, base_head)
        document = json.loads(self.edition_path.read_text(encoding="utf-8"))
        edition = validate_edition(document)
        _check_identity(edition, parsed)
        report = assess_new_candidate(edition)
        policy_ids = {item["id"] for item in report["rejected"]}
        editor_ids = {item["id"] for item in editorial}
        allowed_removals = policy_ids | editor_ids
        for event_id in parsed["approved_removals"]:
            if event_id not in allowed_removals:
                raise EditionError(
                    f"refusing to remove event that was not rejected: {event_id}"
                )
        for change in parsed["approved_wording"]:
            if change["event_id"] not in editor_ids:
                raise EditionError("wording requires an editorial rejection for that event")
        revised_events = []
        original_ids = [event["id"] for event in edition["events"]]
        removal_ids = set(parsed["approved_removals"])
        for event in edition["events"]:
            updated = _apply_wording(event, parsed["approved_wording"])
            if updated["id"] in removal_ids:
                continue
            revised_events.append(updated)
        if [event["id"] for event in revised_events] != [
            event_id for event_id in original_ids if event_id not in removal_ids
        ]:
            raise EditionError("revision changed the event order")
        block = execution_block(parsed.get("execution_evidence"))
        if not revised_events:
            failed = dict(edition)
            failed.pop("content_hash", None)
            failed["events"] = []
            failed["status"] = "failed"
            failed["failure"] = _empty_failure(report, parsed["approved_removals"], editorial)
            failed_edition = validate_edition(failed)
            self._write_edition(failed_edition)
            if self.html_path.exists():
                self.html_path.unlink()
            return _result(failed_edition, block, html_written=False)
        published = dict(edition)
        published.pop("content_hash", None)
        published["events"] = revised_events
        published["status"] = "published_candidate"
        published["failure"] = None
        published_edition = validate_edition(published)
        require_new_candidate(published_edition)
        html = render_web_edition(published_edition)
        self._publish_pair(published_edition, html)
        return _result(published_edition, block, html_written=True)

    def bind_reviewed_head(
        self,
        *,
        edition_id: str,
        attempt: int,
        revision: int,
        reviewed_head: str,
        content_hash: str,
    ) -> dict[str, Any]:
        reviewed = parse_git_head(reviewed_head, "revision.reviewed_head")
        with self._lock():
            state = self._read_state()
            key = f"{edition_id}|{attempt}|{revision}"
            existing = state["completions"].get(key)
            if not existing or existing.get("status") != "applied" or not existing.get("result"):
                raise EditionError("revision completion is missing")
            result = existing["result"]
            if result["content_hash"] != content_hash:
                raise EditionError("final review hash does not match the revision")
            bound = result.get("reviewed_head")
            if bound is None:
                result["reviewed_head"] = reviewed
                self._write_state(state)
                return result
            if bound != reviewed:
                raise EditionError("reviewed head does not match the bound final head")
            return result

    def _remember_edition(self) -> None:
        self._edition_backup = (
            self.edition_path.read_bytes() if self.edition_path.exists() else None
        )
        self._html_backup = self.html_path.read_bytes() if self.html_path.exists() else None

    def _restore_edition(self) -> None:
        if not hasattr(self, "_edition_backup"):
            return
        if self._edition_backup is None:
            self.edition_path.unlink(missing_ok=True)
        else:
            self.edition_path.write_bytes(self._edition_backup)
        if self._html_backup is None:
            self.html_path.unlink(missing_ok=True)
        else:
            self.html_path.write_bytes(self._html_backup)

    def _publish_pair(self, edition: Mapping[str, Any], html: str) -> None:
        edition_tmp = self.root / "edition.json.new"
        html_tmp = self.root / "index.html.new"
        edition_tmp.write_text(
            json.dumps(edition, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        try:
            self._write_html_text(html_tmp, html)
        except Exception:
            edition_tmp.unlink(missing_ok=True)
            html_tmp.unlink(missing_ok=True)
            raise
        html_tmp.replace(self.html_path)
        edition_tmp.replace(self.edition_path)

    def _write_html_text(self, path: Path, html: str) -> None:
        path.write_text(html, encoding="utf-8")

    def _log_path(self) -> Path:
        return self.root / "revisions.json"

    def _read_log(self) -> dict[str, Any]:
        path = self._log_path()
        if not path.exists():
            return {"version": 1, "records": []}
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("version") != 1 or not isinstance(payload.get("records"), list):
            raise EditionError("invalid revision log")
        return payload

    def _revision_already_logged(self, revision: int) -> bool:
        return any(record.get("revision") == revision for record in self._read_log()["records"])

    def _note_revision(self, parsed: Mapping[str, Any], status: str, reason: str | None) -> None:
        log = self._read_log()
        records = log["records"]
        current = next(
            (
                record
                for record in records
                if record.get("revision") == parsed["revision"]
                and record.get("attempt") == parsed["attempt"]
            ),
            None,
        )
        entry = {
            "edition_id": parsed["edition_id"],
            "attempt": parsed["attempt"],
            "revision": parsed["revision"],
            "base_head": parsed["base_head"],
            "base_content_hash": parsed["base_content_hash"],
            "status": status,
            "reason": reason,
        }
        if current is None:
            if records and parsed["revision"] <= max(record["revision"] for record in records):
                raise EditionError("revision numbers must increase")
            records.append(entry)
        else:
            current.update(entry)
        temporary = self._log_path().with_suffix(".json.new")
        temporary.write_text(json.dumps(log, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self._log_path())

    def _read_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"version": 1, "completions": {}}
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        if payload.get("version") != 1 or not isinstance(payload.get("completions"), dict):
            raise EditionError("invalid revision state")
        return payload

    def _write_state(self, payload: Mapping[str, Any]) -> None:
        temporary = self.state_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.state_path)

    def _write_edition(self, edition: Mapping[str, Any]) -> None:
        temporary = self.edition_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(edition, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.edition_path)


def require_head_agreement(claimed: str, observed: str) -> None:
    """Compare a caller head with the head observed from git. This does not call GitHub."""
    if parse_git_head(claimed, "revision.current_head") != parse_git_head(
        observed, "revision.observed_head"
    ):
        raise EditionError("current head does not match the observed head")


def _edition_delivery_frozen(ledger: DeliveryLedger, edition_id: str) -> bool:
    payload = ledger._read()
    if DeliveryLedger._frozen_hash(payload, edition_id):
        return True
    for record in payload["records"].values():
        stored = record.get("key") or {}
        if stored.get("edition_id") == edition_id and record.get("status") == "sent":
            return True
    return False


def revision_log_extends(
    baseline: str | None,
    current: str,
    *,
    edition_id: str | None = None,
) -> None:
    """A committed revision log may only grow by appending new numbers."""
    try:
        current_records = json.loads(current).get("records")
    except json.JSONDecodeError as exc:
        raise EditionError("invalid revision log") from exc
    if not isinstance(current_records, list):
        raise EditionError("invalid revision log")
    _validate_revision_records(current_records, edition_id)
    if baseline is None:
        return
    try:
        baseline_records = json.loads(baseline).get("records")
    except json.JSONDecodeError as exc:
        raise EditionError("invalid revision log") from exc
    if not isinstance(baseline_records, list):
        raise EditionError("invalid revision log")
    _validate_revision_records(baseline_records, edition_id)
    if current_records[: len(baseline_records)] != baseline_records:
        raise EditionError("revision history was rewritten")
    if len(current_records) < len(baseline_records):
        raise EditionError("revision history was rewritten")


def _validate_revision_records(records: list[Any], edition_id: str | None) -> None:
    previous: int | None = None
    for record in records:
        if not isinstance(record, Mapping):
            raise EditionError("invalid revision record")
        revision = record.get("revision")
        if type(revision) is not int or not 1 <= revision <= MAX_REVISION:
            raise EditionError("revision number is invalid")
        if previous is not None and revision <= previous:
            raise EditionError("revision numbers must increase")
        previous = revision
        status = record.get("status")
        if status not in {"attempted", "applied", "blocked"}:
            raise EditionError("revision status is invalid")
        record_edition = record.get("edition_id")
        if not isinstance(record_edition, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", record_edition):
            raise EditionError("revision edition_id is invalid")
        if edition_id is not None and record_edition != edition_id:
            raise EditionError("revision edition_id does not match the directory")
        attempt = record.get("attempt")
        if type(attempt) is not int or attempt < 1:
            raise EditionError("revision attempt is invalid")
        head = record.get("base_head")
        if not isinstance(head, str) or not _GIT_HEAD.fullmatch(head):
            raise EditionError("revision base_head is invalid")


def parse_git_head(value: Any, label: str) -> str:
    """Accept a 40-digit hex head. This does not look the object up."""
    text = _text(value, label).lower()
    if not _GIT_HEAD.fullmatch(text):
        raise EditionError(f"{label} must be a 40-digit hexadecimal git head")
    return text


def execution_block(evidence: str | None) -> dict[str, Any]:
    """No in-repo value is a platform execution id. bc- ids are not stored."""
    if evidence is None or not str(evidence).strip():
        reason = "missing_reliable_execution_evidence"
    elif str(evidence).strip().startswith("bc-"):
        reason = "agent_id_is_not_execution_id"
    else:
        reason = "unverified_execution_evidence"
    return {
        "platform_continuation": "blocked",
        "execution_block_reason": reason,
        "execution_id": None,
    }


def _parse_contract(contract: Mapping[str, Any]) -> dict[str, Any]:
    edition_id = _text(contract.get("edition_id"), "revision.edition_id")
    base_head = _text(contract.get("base_head"), "revision.base_head")
    base_hash = _text(contract.get("base_content_hash"), "revision.base_content_hash")
    attempt = contract.get("attempt")
    revision = contract.get("revision")
    if type(attempt) is not int or attempt < 1:
        raise EditionError("revision.attempt must be an integer >= 1")
    if type(revision) is not int or revision < 1:
        raise EditionError("revision.revision must be an integer >= 1")
    removals = contract.get("approved_removals", [])
    wording = contract.get("approved_wording", [])
    editorial = contract.get("editorial_rejections", [])
    if not isinstance(removals, list) or not all(isinstance(item, str) and item.strip() for item in removals):
        raise EditionError("revision.approved_removals must be a list of event ids")
    if not isinstance(wording, list):
        raise EditionError("revision.approved_wording must be a list")
    if not isinstance(editorial, list):
        raise EditionError("revision.editorial_rejections must be a list")
    return {
        "edition_id": edition_id,
        "attempt": attempt,
        "revision": revision,
        "base_head": base_head,
        "base_content_hash": base_hash,
        "approved_removals": [item.strip() for item in removals],
        "approved_wording": [_parse_wording(item) for item in wording],
        "editorial_rejections": [_parse_editorial(item) for item in editorial],
        "execution_evidence": contract.get("execution_evidence"),
    }


def _parse_editorial(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise EditionError("editorial rejection must be an object")
    reasons = value.get("reasons")
    if not isinstance(reasons, list) or not reasons or not all(
        isinstance(item, str) and item.strip() for item in reasons
    ):
        raise EditionError("editorial rejection requires reasons")
    return {
        "id": _text(value.get("id"), "editorial.id"),
        "reasons": [item.strip() for item in reasons],
        "edition_id": _text(value.get("edition_id"), "editorial.edition_id"),
        "attempt": value.get("attempt"),
        "base_head": _text(value.get("base_head"), "editorial.base_head"),
    }


def _bind_editorial(
    editorial: list[dict[str, Any]],
    contract: Mapping[str, Any],
    base_head: str,
) -> list[dict[str, Any]]:
    bound: list[dict[str, Any]] = []
    for item in editorial:
        head = parse_git_head(item["base_head"], "editorial.base_head")
        if item["edition_id"] != contract["edition_id"] or item["attempt"] != contract["attempt"]:
            raise EditionError("editorial rejection is not bound to this edition")
        if head != base_head:
            raise EditionError("editorial rejection is not bound to this head")
        bound.append({**item, "base_head": head})
    return bound


def _empty_failure(
    report: Mapping[str, Any],
    removals: list[str],
    editorial: list[dict[str, Any]],
) -> dict[str, str]:
    policy_ids = {item["id"] for item in report["rejected"]}
    if any(event_id in policy_ids for event_id in removals):
        return evidence_failure(
            [item for item in report["rejected"] if item["id"] in removals] or report["rejected"]
        )
    ids = ", ".join(item["id"] for item in editorial if item["id"] in removals)
    return {"stage": "editorial", "message": f"editor rejected every remaining event: {ids}"}


def _parse_wording(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise EditionError("revision wording must be an object")
    field = value.get("field")
    if field not in WORDING_FIELDS:
        raise EditionError("wording field is not a reader field")
    parsed: dict[str, Any] = {
        "event_id": _text(value.get("event_id"), "wording.event_id"),
        "field": field,
        "value": _text(value.get("value"), "wording.value"),
    }
    if field == "fact":
        index = value.get("index")
        if type(index) is not int or index < 0:
            raise EditionError("wording fact index is outside the event")
        parsed["index"] = index
    return parsed


def _apply_wording(event: Mapping[str, Any], wording: list[dict[str, Any]]) -> dict[str, Any]:
    updated = dict(event)
    updated["facts"] = list(event["facts"])
    for change in wording:
        if change["event_id"] != event["id"]:
            continue
        field = change["field"]
        if field == "fact":
            index = change["index"]
            if index >= len(updated["facts"]):
                raise EditionError("wording fact index is outside the event")
            updated["facts"][index] = change["value"]
        else:
            updated[field] = change["value"]
    return updated


def _check_identity(edition: Mapping[str, Any], contract: Mapping[str, Any]) -> None:
    if edition["edition_id"] != contract["edition_id"]:
        raise EditionError("revision edition_id does not match")
    if edition["attempt"] != contract["attempt"]:
        raise EditionError("revision attempt does not match")
    if edition["content_hash"] != contract["base_content_hash"]:
        raise EditionError("base content hash mismatch")


def _completion_key(contract: Mapping[str, Any]) -> str:
    return f"{contract['edition_id']}|{contract['attempt']}|{contract['revision']}"


def _same_contract(stored: Mapping[str, Any], parsed: Mapping[str, Any]) -> bool:
    return stored == parsed


def _result(edition: Mapping[str, Any], block: Mapping[str, Any], *, html_written: bool) -> dict[str, Any]:
    return {
        "edition_id": edition["edition_id"],
        "attempt": edition["attempt"],
        "status": edition["status"],
        "content_hash": edition["content_hash"],
        "failure": edition.get("failure"),
        "html_written": html_written,
        "reviewed_head": None,
        **block,
    }


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EditionError(f"{label} must be a non-empty string")
    return value.strip()
