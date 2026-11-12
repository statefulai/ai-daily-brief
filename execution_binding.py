"""Reference contract for binding one dispatch to one run.

This module does not call a platform API and does not read secrets. A bot
outside the repository can mirror these checks. ``source`` is a string the
caller reports. Nothing here checks that a wake payload or a run query
actually produced it. A head is a 40-digit hex string on the evidence. It is
not fetched from GitHub. Dispatch stores ``base_head`` (``expected_head``
is the same value on older receipts). Completion stores ``result_head`` and
may differ from ``base_head``. ``require_result_head`` compares that produced
head with a remote pull request ``headRefOid`` supplied by the caller.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

ACCEPTED_SOURCES = frozenset({"wake_payload", "run_query"})
TERMINAL_STATES = frozenset({"finished", "failed"})
_HEAD = re.compile(r"^[0-9a-f]{40}$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


class BindingError(ValueError):
    """Raised when a dispatch receipt itself is unusable."""


class RunLog:
    def __init__(self) -> None:
        self.dispatches: list[dict[str, Any]] = []
        self.completions: dict[str, dict[str, Any]] = {}

    def dispatch(self, receipt: Mapping[str, Any]) -> dict[str, Any]:
        run_id = _own_run_id(receipt)
        edition_id = receipt.get("edition_id")
        base_head = receipt.get("base_head")
        if not isinstance(base_head, str):
            base_head = receipt.get("expected_head")
        if not isinstance(edition_id, str) or not edition_id.strip():
            raise BindingError("dispatch requires edition_id")
        if not isinstance(base_head, str) or not _HEAD.fullmatch(base_head):
            raise BindingError("dispatch requires expected_head")
        if any(item["run_id"] == run_id for item in self.dispatches):
            raise BindingError("dispatch run id is already stored")
        record = {
            "run_id": run_id,
            "edition_id": edition_id,
            "base_head": base_head,
            "expected_head": base_head,
            "status": "dispatched",
            "head": None,
            "result_head": None,
            "content_hash": None,
        }
        self.dispatches.append(record)
        return dict(record)

    def complete(self, evidence: Mapping[str, Any]) -> dict[str, Any]:
        missing = _missing_completion_fields(evidence)
        if missing:
            return {"status": "blocked", "missing": missing, "execution_id": None}
        run_id = str(evidence.get("run_id"))
        source = evidence.get("source")
        if source not in ACCEPTED_SOURCES:
            return {
                "status": "rejected",
                "reason": "completion source is not a per-run receipt",
                "execution_id": None,
            }
        prefix = _prefix_problem(run_id)
        if prefix:
            return {"status": "rejected", "reason": prefix, "execution_id": None}
        known = [item for item in self.dispatches if item["run_id"] == run_id]
        if not known:
            return {"status": "rejected", "reason": "completion run was not dispatched", "execution_id": None}
        if not _is_latest_in_lane(self.dispatches, known[0]):
            return {"status": "rejected", "reason": "late wake from an older run", "execution_id": None}
        if run_id in self.completions:
            return {"status": "rejected", "reason": "duplicate wake", "execution_id": None}
        terminal = evidence.get("terminal_state")
        if terminal not in TERMINAL_STATES:
            return {"status": "blocked", "missing": ["terminal_state"], "execution_id": None}
        head = _produced_head(evidence)
        content_hash = evidence.get("content_hash")
        if not isinstance(head, str) or not _HEAD.fullmatch(head):
            return {"status": "rejected", "reason": "head must be 40 hexadecimal digits", "execution_id": None}
        if not isinstance(content_hash, str) or not _HASH.fullmatch(content_hash):
            return {"status": "rejected", "reason": "content_hash must be sha256 and 64 hexadecimal digits", "execution_id": None}
        base_head = known[0].get("base_head")
        record = {
            "status": "accepted",
            "run_id": run_id,
            "terminal_state": terminal,
            "source": source,
            "source_is_self_reported": True,
            "base_head": base_head,
            "result_head": head,
            "head": head,
            "content_hash": content_hash,
            "execution_id": run_id,
        }
        self.completions[run_id] = record
        known[0]["status"] = terminal
        known[0]["head"] = head
        known[0]["result_head"] = head
        known[0]["content_hash"] = content_hash
        return dict(record)

    def require_result_head(self, run_id: str, head_ref_oid: str) -> dict[str, Any]:
        """Block unless this run's result_head equals the remote PR headRefOid.

        The caller passes ``headRefOid``. This method does not fetch it.
        A missing result_head and a different head both block.
        """
        completion = self.completions.get(run_id)
        result_head = None if not isinstance(completion, Mapping) else completion.get("result_head")
        if not isinstance(result_head, str) or not _HEAD.fullmatch(result_head):
            return {"status": "blocked", "reason": "result_head is missing", "execution_id": None}
        if not isinstance(head_ref_oid, str) or not _HEAD.fullmatch(head_ref_oid) or result_head != head_ref_oid:
            return {
                "status": "blocked",
                "reason": "result_head does not match headRefOid",
                "execution_id": None,
            }
        return {
            "status": "accepted",
            "run_id": run_id,
            "base_head": completion.get("base_head"),
            "result_head": result_head,
            "head_ref_oid": head_ref_oid,
            "execution_id": run_id,
        }


def _own_run_id(receipt: Mapping[str, Any]) -> str:
    if "latest_run_id" in receipt or "agent_status" in receipt:
        raise BindingError("dispatch receipt must not use agent status or latestRunId")
    run_id = receipt.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise BindingError("dispatch receipt has no run id")
    problem = _prefix_problem(run_id.strip())
    if problem:
        raise BindingError(problem)
    if receipt.get("agent_id") and receipt.get("agent_id") == run_id:
        raise BindingError("agent id is not a run id")
    return run_id.strip()


def _prefix_problem(run_id: str) -> str | None:
    if run_id.startswith("bc-"):
        return "agent id is not a run id"
    if run_id == "run-":
        return "run id has an empty suffix"
    if not run_id.startswith("run-"):
        return "run id prefix is unknown"
    return None


def _is_latest_in_lane(dispatches: list[dict[str, Any]], record: Mapping[str, Any]) -> bool:
    edition_id = record.get("edition_id")
    if not isinstance(edition_id, str) or not edition_id:
        return False
    lane = [item for item in dispatches if item.get("edition_id") == edition_id]
    return bool(lane) and lane[-1]["run_id"] == record["run_id"]


def _produced_head(evidence: Mapping[str, Any]) -> Any:
    result_head = evidence.get("result_head")
    if isinstance(result_head, str) and result_head:
        return result_head
    return evidence.get("head")


def _missing_completion_fields(evidence: Mapping[str, Any]) -> list[str]:
    missing: list[str] = []
    if not evidence.get("run_id"):
        missing.append("run_id")
    if not evidence.get("terminal_state"):
        missing.append("terminal_state")
    if not evidence.get("source"):
        missing.append("source")
    if not _produced_head(evidence):
        missing.append("head")
    if not evidence.get("content_hash"):
        missing.append("content_hash")
    return missing
