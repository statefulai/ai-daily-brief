"""Apply one approved editorial revision to a single edition workspace.

The helper removes rejected events and applies approved wording. It does not
collect stories, bump attempt, or mint an execution id. Platform continuation
stays blocked until a reliable execution id exists outside this repository.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
from typing import Any, Iterator, Mapping

from candidate_policy import (
    MAX_REVISION,
    assess_new_candidate,
    evidence_failure,
    evidence_outcome,
    require_new_candidate,
)
from edition import EditionError, validate_edition
from outputs import render_web_edition

WORDING_FIELDS = {"title", "kicker", "lede", "caveat", "fact"}


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

    def apply(self, contract: Mapping[str, Any], *, current_head: str) -> dict[str, Any]:
        parsed = _parse_contract(contract)
        with self._lock():
            state = self._read_state()
            key = _completion_key(parsed)
            existing = state["completions"].get(key)
            if existing:
                if _same_contract(existing["contract"], parsed):
                    return existing["result"]
                raise EditionError("duplicate revision completion does not match")
            if current_head != parsed["base_head"]:
                raise EditionError("stale revision head")
            document = json.loads(self.edition_path.read_text(encoding="utf-8"))
            edition = validate_edition(document)
            _check_identity(edition, parsed)
            report = assess_new_candidate(edition)
            rejected_ids = {item["id"] for item in report["rejected"]}
            for event_id in parsed["approved_removals"]:
                if event_id not in rejected_ids:
                    raise EditionError(
                        f"refusing to remove event that was not rejected: {event_id}"
                    )
            revised_events = []
            original_ids = [event["id"] for event in edition["events"]]
            for event in edition["events"]:
                updated = _apply_wording(event, parsed["approved_wording"])
                if updated["id"] in parsed["approved_removals"]:
                    continue
                revised_events.append(updated)
            if [event["id"] for event in revised_events] != [
                event_id
                for event_id in original_ids
                if event_id not in parsed["approved_removals"]
            ]:
                raise EditionError("revision changed the event order")
            outcome = evidence_outcome(
                edition["sources"],
                retained=len(revised_events),
                rejected=len(parsed["approved_removals"]),
            )
            block = execution_block(parsed.get("execution_evidence"))
            if outcome == "failed":
                failed = dict(edition)
                failed.pop("content_hash", None)
                failed["events"] = []
                failed["status"] = "failed"
                failed["failure"] = evidence_failure(
                    [item for item in report["rejected"] if item["id"] in parsed["approved_removals"]]
                    or report["rejected"]
                )
                failed_edition = validate_edition(failed)
                self._write_edition(failed_edition)
                if self.html_path.exists():
                    self.html_path.unlink()
                result = _result(failed_edition, block, html_written=False)
            else:
                published = dict(edition)
                published.pop("content_hash", None)
                published["events"] = revised_events
                published["status"] = "published_candidate"
                published["failure"] = None
                published_edition = validate_edition(published)
                require_new_candidate(published_edition)
                html = render_web_edition(published_edition)
                self._write_edition(published_edition)
                self.html_path.write_text(html, encoding="utf-8")
                result = _result(published_edition, block, html_written=True)
            state["completions"][key] = {"contract": parsed, "result": result}
            self._write_state(state)
            return result

    def bind_reviewed_head(
        self,
        *,
        edition_id: str,
        attempt: int,
        revision: int,
        reviewed_head: str,
        content_hash: str,
    ) -> dict[str, Any]:
        with self._lock():
            state = self._read_state()
            key = f"{edition_id}|{attempt}|{revision}"
            existing = state["completions"].get(key)
            if not existing:
                raise EditionError("revision completion is missing")
            result = existing["result"]
            if result["content_hash"] != content_hash:
                raise EditionError("final review hash does not match the revision")
            bound = result.get("reviewed_head")
            if bound is None:
                result["reviewed_head"] = reviewed_head
                self._write_state(state)
                return result
            if bound != reviewed_head:
                raise EditionError("reviewed head does not match the bound final head")
            return result

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
    if type(revision) is not int or not 1 <= revision <= MAX_REVISION:
        raise EditionError(f"revision is outside the bound of {MAX_REVISION}")
    removals = contract.get("approved_removals", [])
    wording = contract.get("approved_wording", [])
    if not isinstance(removals, list) or not all(isinstance(item, str) and item.strip() for item in removals):
        raise EditionError("revision.approved_removals must be a list of event ids")
    if not isinstance(wording, list):
        raise EditionError("revision.approved_wording must be a list")
    return {
        "edition_id": edition_id,
        "attempt": attempt,
        "revision": revision,
        "base_head": base_head,
        "base_content_hash": base_hash,
        "approved_removals": [item.strip() for item in removals],
        "approved_wording": [_parse_wording(item) for item in wording],
        "execution_evidence": contract.get("execution_evidence"),
    }


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
