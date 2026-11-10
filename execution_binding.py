"""Reference contract for binding one dispatch to one run.

This module does not call a platform API and does not read secrets. A bot
outside the repository can mirror these checks. A 40-digit head recorded here
is the head the completion evidence names. It is not fetched from GitHub.
"""

from __future__ import annotations

from typing import Any, Mapping

ACCEPTED_SOURCES = frozenset({"wake_payload", "run_query"})
TERMINAL_STATES = frozenset({"finished", "failed"})


class BindingError(ValueError):
    """Raised when a dispatch receipt itself is unusable."""


class RunLog:
    def __init__(self) -> None:
        self.dispatches: list[dict[str, Any]] = []
        self.completions: dict[str, dict[str, Any]] = {}

    def dispatch(self, receipt: Mapping[str, Any]) -> dict[str, Any]:
        run_id = _own_run_id(receipt)
        if any(item["run_id"] == run_id for item in self.dispatches):
            raise BindingError("dispatch run id is already stored")
        record = {
            "run_id": run_id,
            "status": "dispatched",
            "head": None,
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
        if self.dispatches[-1]["run_id"] != run_id:
            return {"status": "rejected", "reason": "late wake from an older run", "execution_id": None}
        if run_id in self.completions:
            return {"status": "rejected", "reason": "duplicate wake", "execution_id": None}
        terminal = evidence.get("terminal_state")
        if terminal not in TERMINAL_STATES:
            return {"status": "blocked", "missing": ["terminal_state"], "execution_id": None}
        head = evidence.get("head")
        content_hash = evidence.get("content_hash")
        if not isinstance(head, str) or not isinstance(content_hash, str):
            return {"status": "blocked", "missing": ["head", "content_hash"], "execution_id": None}
        record = {
            "status": "accepted",
            "run_id": run_id,
            "terminal_state": terminal,
            "source": source,
            "head": head,
            "content_hash": content_hash,
            "execution_id": run_id,
        }
        self.completions[run_id] = record
        known[0]["status"] = terminal
        known[0]["head"] = head
        known[0]["content_hash"] = content_hash
        return dict(record)


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
    if not run_id.startswith("run-"):
        return "run id prefix is unknown"
    return None


def _missing_completion_fields(evidence: Mapping[str, Any]) -> list[str]:
    missing: list[str] = []
    if not evidence.get("run_id"):
        missing.append("run_id")
    if not evidence.get("terminal_state"):
        missing.append("terminal_state")
    if not evidence.get("source"):
        missing.append("source")
    if not evidence.get("head"):
        missing.append("head")
    if not evidence.get("content_hash"):
        missing.append("content_hash")
    return missing
