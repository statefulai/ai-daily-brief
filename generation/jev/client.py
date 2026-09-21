"""TypeSafe System One HTTP client. Reuses the verified experiment request shape."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable

API_URL = "https://api.typesafe.ai/v1/systemone"
USER_AGENT = "ai-daily-brief-jev-assist/1.0"

Opener = Callable[..., Any]


class JevClientError(Exception):
    """A single request failed. Assist must not retry it automatically."""

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        http_status: int | None = None,
        ambiguous: bool = False,
    ):
        super().__init__(message)
        self.kind = kind
        self.http_status = http_status
        self.ambiguous = ambiguous


def _api_key(environ: dict[str, str] | None = None) -> str:
    import os

    env = environ if environ is not None else os.environ
    value = env.get("TYPESAFE_API_KEY", "")
    return value.strip() if isinstance(value, str) else ""


def parse_answers(response: Any) -> dict[str, Any]:
    """Accept only the documented System One answer shape for our two questions."""
    if not isinstance(response, dict):
        raise JevClientError("invalid_response", "response is not an object", ambiguous=True)
    answers = response.get("answers")
    if not isinstance(answers, dict):
        raise JevClientError("invalid_response", "missing answers object", ambiguous=True)
    new_fact = answers.get("has_clear_new_fact")
    reader_value = answers.get("reader_value")
    if not isinstance(new_fact, dict) or new_fact.get("type") != "noul":
        raise JevClientError("invalid_response", "has_clear_new_fact is not a noul", ambiguous=True)
    noul = new_fact.get("noul")
    if not isinstance(noul, (int, float)) or isinstance(noul, bool) or not 0 <= float(noul) <= 1:
        raise JevClientError("invalid_response", "has_clear_new_fact.noul is invalid", ambiguous=True)
    if not isinstance(reader_value, dict) or reader_value.get("type") != "score":
        raise JevClientError("invalid_response", "reader_value is not a score", ambiguous=True)
    score = reader_value.get("score")
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        raise JevClientError("invalid_response", "reader_value.score is invalid", ambiguous=True)
    return {
        "has_clear_new_fact": {
            "type": "noul",
            "noul": float(noul),
        },
        "reader_value": {
            "type": "score",
            "score": float(score),
            "confidence": reader_value.get("confidence"),
            "legend": reader_value.get("legend"),
            "probabilities": reader_value.get("probabilities"),
        },
    }


def _classify_http_error(code: int) -> str:
    if code in {401, 403}:
        return "auth"
    if code == 429:
        return "rate_limit"
    if code == 529:
        return "overloaded"
    return "http_error"


def call_jev(
    *,
    api_key: str,
    model: str,
    state: Any,
    questions: dict[str, Any],
    timeout: float,
    opener: Opener | None = None,
) -> tuple[dict[str, Any], float]:
    """POST one candidate. Never logs the API key. No automatic retries."""
    if not api_key:
        raise JevClientError("missing_key", "TYPESAFE_API_KEY is missing")
    body = json.dumps({"model": model, "state": state, "questions": questions}).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    open_url = opener or urllib.request.urlopen
    t0 = time.perf_counter()
    try:
        with open_url(req, timeout=timeout) as resp:
            raw = resp.read()
            elapsed = time.perf_counter() - t0
    except JevClientError:
        raise
    except urllib.error.HTTPError as exc:
        # Consume the body so the socket can close; do not persist or log secrets.
        try:
            exc.read()
        except Exception:  # noqa: BLE001
            pass
        raise JevClientError(
            _classify_http_error(exc.code),
            f"HTTP {exc.code}",
            http_status=exc.code,
        ) from None
    except TimeoutError as exc:
        raise JevClientError("timeout", "request timed out", ambiguous=True) from exc
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            raise JevClientError("timeout", "request timed out", ambiguous=True) from exc
        raise JevClientError("http_error", f"{type(reason).__name__}: {reason}") from exc
    except Exception as exc:  # noqa: BLE001
        name = type(exc).__name__
        if "timeout" in name.lower() or "timed out" in str(exc).lower():
            raise JevClientError("timeout", "request timed out", ambiguous=True) from exc
        raise JevClientError("http_error", f"{name}") from exc

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JevClientError("invalid_response", "response is not JSON", ambiguous=True) from exc
    answers = parse_answers(payload)
    payload = dict(payload)
    payload["answers"] = answers
    return payload, elapsed
