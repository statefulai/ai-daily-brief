"""Assist-only Jev scoring: quota, reuse, fail-open. Never selects or empties an edition."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
import logging
import os
import time
from pathlib import Path
from typing import Any

from generation.jev.client import JevClientError, Opener, _api_key, call_jev
from generation.jev.fingerprint import reuse_fingerprint, rubric_id
from generation.jev.merge import has_required_fields, merge_same_event
from generation.jev.prior import load_prior_coverage
from generation.jev.rubric import DEFAULT_MODEL, build_questions, build_state
from generation.jev.store import (
    DEFAULT_LIMIT,
    JevRunStore,
    StoreError,
    beijing_calendar_date,
    resolve_store_location,
)

logger = logging.getLogger(__name__)

# Canonical operator kill switch. Checked before any HTTP. Aliases still work
# only when JEV_ASSIST is unset.
CANONICAL_DISABLE_ENV = "JEV_ASSIST"
FALSEY = {"0", "false", "off", "no", "disabled"}
TRUTHY = {"1", "true", "yes", "on"}


def jev_enabled(config: dict[str, Any] | None = None, environ: dict[str, str] | None = None) -> bool:
    """Return False when the operator disabled assist. Canonical: JEV_ASSIST=0."""
    env = environ if environ is not None else os.environ
    raw = env.get(CANONICAL_DISABLE_ENV)
    if isinstance(raw, str) and raw.strip():
        return raw.strip().lower() not in FALSEY
    disabled = env.get("JEV_ASSIST_DISABLED", "")
    if isinstance(disabled, str) and disabled.strip().lower() in TRUTHY:
        return False
    config = config or {}
    return bool(config.get("enabled", True))


def daily_request_limit(config: dict[str, Any] | None = None) -> int:
    config = config or {}
    raw = config.get("daily_request_limit", DEFAULT_LIMIT)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_LIMIT
    return max(0, min(DEFAULT_LIMIT, value))


def _annotation_from_record(record: dict[str, Any], *, reused: bool) -> dict[str, Any]:
    status = record.get("status") or "error"
    note: dict[str, Any] = {
        "status": status,
        "reused": reused,
        "assist_only": True,
    }
    if record.get("answers"):
        note["answers"] = record["answers"]
    if record.get("error"):
        note["reason"] = record["error"]
    if record.get("model"):
        note["model"] = record["model"]
    if status == "in_flight":
        note["status"] = "ambiguous"
        note["reason"] = record.get("error") or "in_flight"
    return note


@dataclass
class AssistResult:
    candidates: list[dict[str, Any]]
    summary: dict[str, Any] = field(default_factory=dict)

    @property
    def skipped(self) -> bool:
        return bool(self.summary.get("skipped"))


def _skip(
    candidates: list[dict[str, Any]],
    reason: str,
    *,
    extra: dict[str, Any] | None = None,
) -> AssistResult:
    summary = {
        "skipped": True,
        "reason": reason,
        "assist_only": True,
        "scored": 0,
        "reused": 0,
        "unscored": len(candidates),
        "real_requests": 0,
    }
    if extra:
        summary.update(extra)
    store_error = (extra or {}).get("store_error")
    if store_error:
        logger.info("Jev assist skipped: %s (%s)", reason, store_error)
    else:
        logger.info("Jev assist skipped: %s", reason)
    return AssistResult(candidates=list(candidates), summary=summary)


def _open_store(
    *,
    config: dict[str, Any],
    environ: dict[str, str],
    store: JevRunStore | None,
    now: datetime | None,
    repo_root: str | Path | None,
) -> JevRunStore:
    if store is not None:
        store.restore()
        return store
    date = beijing_calendar_date(now)
    location = resolve_store_location(
        now=now,
        environ=environ,
        explicit=config.get("store_path"),
        repo_root=repo_root if repo_root is not None else config.get("repo_root"),
    )
    opened = JevRunStore(
        location.path,
        calendar_date=date,
        request_limit=daily_request_limit(config),
        allow_create=location.allow_create,
    )
    opened.restore()
    return opened


def assist_candidates(
    candidates: list[dict[str, Any]],
    *,
    config: dict[str, Any] | None = None,
    store: JevRunStore | None = None,
    now: datetime | None = None,
    environ: dict[str, str] | None = None,
    prior_coverage: list[dict[str, Any]] | None = None,
    opener: Opener | None = None,
    repo_root: str | Path | None = None,
) -> AssistResult:
    """Score merged candidates. Fail open. Never drop or auto-select rows."""
    config = dict(config or {})
    env = environ if environ is not None else os.environ
    incoming = [deepcopy(item) for item in candidates if isinstance(item, dict)]
    # Canonical disable is JEV_ASSIST=0; checked before store I/O or HTTP.
    if not jev_enabled(config, env):
        return _skip(incoming, "disabled")
    api_key = _api_key(env)
    if not api_key:
        return _skip(incoming, "missing_key")

    try:
        run_store = _open_store(
            config=config,
            environ=env,
            store=store,
            now=now,
            repo_root=repo_root,
        )
    except StoreError as exc:
        return _skip(incoming, "store_unavailable", extra={"store_error": str(exc)})

    merged = merge_same_event(incoming)
    questions = build_questions(config.get("rubric") if isinstance(config.get("rubric"), dict) else {})
    questions_id = rubric_id(questions)
    model = str(config.get("model") or DEFAULT_MODEL)
    request_timeout = float(config.get("request_timeout_sec") or 30)
    batch_timeout = float(config.get("batch_timeout_sec") or 180)
    prior = prior_coverage
    if prior is None:
        prior = load_prior_coverage(
            config.get("editions_dir") or "editions",
            exclude_edition_id=config.get("exclude_edition_id"),
        )

    started = time.monotonic()
    stop_reason: str | None = None
    annotated: list[dict[str, Any]] = []
    scored = 0
    reused = 0
    real_requests = 0

    for raw in merged:
        item = deepcopy(raw)
        if stop_reason:
            item["jev_assist"] = {
                "status": "skipped",
                "reason": stop_reason,
                "assist_only": True,
            }
            annotated.append(item)
            continue
        if not has_required_fields(item):
            item["jev_assist"] = {
                "status": "skipped",
                "reason": "missing_required_fields",
                "assist_only": True,
            }
            annotated.append(item)
            continue
        if time.monotonic() - started >= batch_timeout:
            stop_reason = "batch_timeout"
            item["jev_assist"] = {
                "status": "skipped",
                "reason": stop_reason,
                "assist_only": True,
            }
            annotated.append(item)
            continue

        if not item.get("prior_coverage"):
            item["prior_coverage"] = prior

        fingerprint = reuse_fingerprint(item, questions_id)
        try:
            claim = run_store.claim(fingerprint, now=now)
        except StoreError:
            stop_reason = "store_unavailable"
            item["jev_assist"] = {
                "status": "skipped",
                "reason": stop_reason,
                "assist_only": True,
            }
            annotated.append(item)
            continue

        if claim.kind == "reuse":
            note = _annotation_from_record(claim.record or {}, reused=True)
            item["jev_assist"] = note
            if note.get("status") == "ok":
                reused += 1
                scored += 1
            annotated.append(item)
            continue
        if claim.kind == "quota_exhausted":
            stop_reason = "quota_exhausted"
            item["jev_assist"] = {
                "status": "skipped",
                "reason": stop_reason,
                "assist_only": True,
            }
            annotated.append(item)
            continue

        remaining = batch_timeout - (time.monotonic() - started)
        timeout = min(request_timeout, max(0.05, remaining))
        real_requests += 1
        try:
            response, _elapsed = call_jev(
                api_key=api_key,
                model=model,
                state=build_state(
                    item,
                    config.get("rubric") if isinstance(config.get("rubric"), dict) else {},
                ),
                questions=questions,
                timeout=timeout,
                opener=opener,
            )
            answers = response.get("answers") if isinstance(response, dict) else None
            resolved_model = response.get("model") if isinstance(response, dict) else None
            run_store.complete(
                fingerprint,
                status="ok",
                answers=answers if isinstance(answers, dict) else None,
                model=resolved_model if isinstance(resolved_model, str) else None,
                now=now,
            )
            item["jev_assist"] = {
                "status": "ok",
                "reused": False,
                "assist_only": True,
                "answers": answers,
                "model": resolved_model,
            }
            scored += 1
        except JevClientError as exc:
            status = "ambiguous" if exc.ambiguous else "error"
            try:
                run_store.complete(
                    fingerprint,
                    status=status,
                    error=exc.kind,
                    now=now,
                )
            except StoreError:
                pass
            item["jev_assist"] = {
                "status": status,
                "reused": False,
                "assist_only": True,
                "reason": exc.kind,
            }
            stop_reason = exc.kind
            logger.info("Jev assist ending this round after %s", exc.kind)
        annotated.append(item)

    try:
        snapshot = run_store.snapshot()
        quota_used = snapshot.get("request_count", 0)
        quota_limit = run_store.request_limit
    except StoreError:
        quota_used = None
        quota_limit = daily_request_limit(config)

    summary = {
        "skipped": False,
        "reason": stop_reason,
        "assist_only": True,
        "scored": scored,
        "reused": reused,
        "unscored": sum(
            1
            for item in annotated
            if (item.get("jev_assist") or {}).get("status") != "ok"
        ),
        "real_requests": real_requests,
        "quota_used": quota_used,
        "quota_limit": quota_limit,
        "model": model,
        "store_path": str(run_store.path),
        "merged_candidates": len(annotated),
    }
    logger.info(
        "Jev assist scored=%s reused=%s requests=%s quota=%s/%s reason=%s",
        scored,
        reused,
        real_requests,
        quota_used,
        quota_limit,
        stop_reason,
    )
    return AssistResult(candidates=annotated, summary=summary)


def load_candidates_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        rows = payload.get("candidates", payload.get("items"))
        if isinstance(rows, list):
            return [item for item in rows if isinstance(item, dict)]
    raise ValueError("candidates payload must be a list or an object with candidates")
