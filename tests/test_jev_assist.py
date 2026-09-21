import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from io import BytesIO
from multiprocessing import Process, Queue
from pathlib import Path
from unittest.mock import patch
import urllib.error

from edition import BEIJING_TZ
from generation.jev.assist import CANONICAL_DISABLE_ENV, assist_candidates, jev_enabled
from generation.jev.client import JevClientError, parse_answers
from generation.jev.fingerprint import (
    evidence_payload,
    reuse_fingerprint,
    rubric_id,
    stable_facts,
)
from generation.jev.merge import merge_same_event
from generation.jev.rubric import build_questions
from generation.jev.store import (
    JevRunStore,
    StoreError,
    beijing_calendar_date,
    resolve_store_location,
)
from scripts.jev_assist import main as jev_assist_main


def ok_payload(noul=0.86, score=2.2):
    return {
        "model": "jev-1.13.0",
        "answers": {
            "has_clear_new_fact": {"type": "noul", "noul": noul},
            "reader_value": {
                "type": "score",
                "score": score,
                "legend": {
                    "0": "Negligible",
                    "1": "Low",
                    "2": "Medium",
                    "3": "High",
                },
                "probabilities": {"0": 0.0, "1": 0.05, "2": 0.8, "3": 0.15},
                "confidence": 0.77,
            },
        },
        "usage": {"input_tokens": 12, "output_tokens": 6},
    }


def candidate(index=1, **overrides):
    item = {
        "id": f"c-{index}",
        "event_key": f"event-{index}",
        "title": f"Title {index}",
        "url": f"https://example.com/{index}",
        "summary": f"Official released capability {index}",
        "facts": [f"official released capability {index}"],
        "prior_coverage": [],
    }
    item.update(overrides)
    return item


class FakeResponse:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class RecordingOpener:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, req, timeout=None):
        with self.lock:
            self.calls.append(
                {
                    "url": req.full_url,
                    "timeout": timeout,
                    "has_authorization_header": "Authorization" in req.headers
                    or req.get_header("Authorization") is not None,
                    "body": json.loads(req.data.decode("utf-8")),
                }
            )
            item = self.responses.pop(0) if self.responses else ok_payload()
        if isinstance(item, Exception):
            raise item
        return FakeResponse(item)


def http_error(code=429):
    return urllib.error.HTTPError(
        "https://api.typesafe.ai/v1/systemone",
        code,
        "error",
        hdrs=None,
        fp=BytesIO(b'{"error":"no"}'),
    )


class JevHelpers:
    DATE = "2026-09-21"
    ENV = {"TYPESAFE_API_KEY": "test-jev-key-do-not-log"}

    def store(self, directory, *, limit=100, allow_create=True):
        path = Path(directory) / "runs" / "jev" / f"quota-{self.DATE}.json"
        store = JevRunStore(
            path,
            calendar_date=self.DATE,
            request_limit=limit,
            allow_create=allow_create,
        )
        store.restore()
        return store

    def beijing_now(self):
        return datetime(2026, 9, 21, 15, 0, tzinfo=BEIJING_TZ)

    def score_shared_dir(self, items, opener, durable_root, repo_root, **kwargs):
        environ = dict(kwargs.pop("environ", self.ENV))
        environ["AI_DAILY_PRIVATE_RUN_DIR"] = str(durable_root)
        return assist_candidates(
            items,
            opener=opener,
            environ=environ,
            repo_root=repo_root,
            now=kwargs.pop("now", self.beijing_now()),
            prior_coverage=kwargs.pop("prior_coverage", []),
            **kwargs,
        )

    def score(self, items, opener, store, **kwargs):
        return assist_candidates(
            items,
            store=store,
            opener=opener,
            environ=kwargs.pop("environ", self.ENV),
            prior_coverage=kwargs.pop("prior_coverage", []),
            **kwargs,
        )


class JevFingerprintTest(unittest.TestCase):
    QUESTIONS_ID = rubric_id(build_questions({}))

    def _item(self, **overrides):
        item = {
            "event_key": "same-event",
            "title": "Title",
            "url": "https://example.com/a",
            "summary": "Official shipped feature A",
            "facts": ["shipped feature A"],
            "prior_coverage": [],
        }
        item.update(overrides)
        return item

    def _fp(self, **overrides):
        return reuse_fingerprint(self._item(**overrides), self.QUESTIONS_ID)

    def test_fact_order_and_duplicate_facts_reuse(self):
        ordered = self._fp(facts=["shipped feature A", "opened waitlist"])
        reversed_facts = self._fp(facts=["opened waitlist", "shipped feature A"])
        duplicated = self._fp(facts=["shipped feature A", "shipped feature A", "opened waitlist"])
        self.assertEqual(ordered, reversed_facts)
        self.assertEqual(ordered, duplicated)
        self.assertEqual(
            evidence_payload(self._item(facts=["opened waitlist", "shipped feature A"]))["facts"],
            ["opened waitlist", "shipped feature a"],
        )

    def test_summary_wording_only_reuses(self):
        baseline = self._fp(summary="Official shipped feature A")
        reworded = self._fp(summary="The company today shipped feature A.")
        chinese = reuse_fingerprint(
            self._item(
                facts=["官方开放了该能力"],
                summary="官方今日开放了该能力。",
            ),
            self.QUESTIONS_ID,
        )
        chinese_same = reuse_fingerprint(
            self._item(
                facts=["官方开放了该能力"],
                summary="官方开放了该能力",
            ),
            self.QUESTIONS_ID,
        )
        self.assertEqual(baseline, reworded)
        self.assertEqual(chinese, chinese_same)
        self.assertEqual(
            stable_facts(self._item(summary="The company today shipped feature A.")),
            ["shipped feature a"],
        )

    def test_material_new_facts_or_corrections_re_evaluate(self):
        baseline = self._fp(facts=["shipped feature A"])
        extra_fact = self._fp(facts=["shipped feature A", "opened a public waitlist"])
        corrected = self._fp(
            facts=["price is $10 per month"],
            summary="Price is $10 per month",
        )
        original_price = self._fp(
            facts=["price is $20 per month"],
            summary="Price is $20 per month",
        )
        self.assertNotEqual(baseline, extra_fact)
        self.assertNotEqual(original_price, corrected)

    def test_summary_material_info_is_folded_into_facts(self):
        baseline = self._item(facts=["shipped feature A"], summary="Official shipped feature A")
        with_new = self._item(
            facts=["shipped feature A"],
            summary="Opened a public waitlist for feature A.",
        )
        self.assertEqual(stable_facts(baseline), ["shipped feature a"])
        self.assertIn("opened a public waitlist for feature a", stable_facts(with_new))
        self.assertNotEqual(
            reuse_fingerprint(baseline, self.QUESTIONS_ID),
            reuse_fingerprint(with_new, self.QUESTIONS_ID),
        )


class JevAssistTest(JevHelpers, unittest.TestCase):
    def test_identical_rerun_reuses_without_second_http(self):
        opener = RecordingOpener()
        with tempfile.TemporaryDirectory() as tmp:
            store = self.store(tmp)
            first = self.score([candidate(1)], opener, store)
            second = self.score([candidate(1)], opener, store)
            self.assertEqual(store.snapshot()["request_count"], 1)

        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(first.candidates[0]["jev_assist"]["status"], "ok")
        self.assertFalse(first.candidates[0]["jev_assist"]["reused"])
        self.assertEqual(second.candidates[0]["jev_assist"]["status"], "ok")
        self.assertTrue(second.candidates[0]["jev_assist"]["reused"])
        self.assertEqual(second.summary["real_requests"], 0)

    def test_title_and_url_only_change_is_not_a_new_candidate(self):
        opener = RecordingOpener()
        first_item = candidate(1, title="Wording A", url="https://news.example/a")
        second_item = candidate(1, title="Wording B", url="https://news.example/b")
        with tempfile.TemporaryDirectory() as tmp:
            store = self.store(tmp)
            self.score([first_item], opener, store)
            self.score([second_item], opener, store)
            merged = self.score([first_item, second_item], opener, store)

        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(len(merged.candidates), 1)

    def test_material_new_evidence_is_re_evaluated(self):
        opener = RecordingOpener()
        with tempfile.TemporaryDirectory() as tmp:
            store = self.store(tmp)
            self.score([candidate(1, facts=["shipped feature A"])], opener, store)
            self.score(
                [candidate(1, facts=["shipped feature A and also opened waitlist"])],
                opener,
                store,
            )
            self.assertEqual(store.snapshot()["request_count"], 2)

        self.assertEqual(len(opener.calls), 2)

    def test_fact_order_duplicate_and_summary_wording_reuse_one_http(self):
        opener = RecordingOpener()
        first = candidate(
            1,
            facts=["shipped feature A", "opened waitlist"],
            summary="Official shipped feature A",
        )
        reordered = candidate(
            1,
            facts=["opened waitlist", "shipped feature A", "shipped feature A"],
            summary="The company today shipped feature A.",
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = self.store(tmp)
            first_result = self.score([first], opener, store)
            reused = self.score([reordered], opener, store)
            self.assertEqual(store.snapshot()["request_count"], 1)

        self.assertEqual(len(opener.calls), 1)
        self.assertFalse(first_result.candidates[0]["jev_assist"]["reused"])
        self.assertTrue(reused.candidates[0]["jev_assist"]["reused"])
        self.assertEqual(reused.summary["real_requests"], 0)

    def test_summary_new_fact_or_correction_makes_a_second_request(self):
        opener = RecordingOpener()
        with tempfile.TemporaryDirectory() as tmp:
            store = self.store(tmp)
            self.score(
                [candidate(1, facts=["shipped feature A"], summary="Official shipped feature A")],
                opener,
                store,
            )
            self.score(
                [
                    candidate(
                        1,
                        facts=["shipped feature A"],
                        summary="Opened a public waitlist for feature A.",
                    )
                ],
                opener,
                store,
            )
            self.score(
                [
                    candidate(
                        1,
                        facts=["price is $20 per month"],
                        summary="Price is $20 per month",
                    )
                ],
                opener,
                store,
            )
            self.score(
                [
                    candidate(
                        1,
                        facts=["price is $10 per month"],
                        summary="Price is $10 per month",
                    )
                ],
                opener,
                store,
            )
            self.assertEqual(store.snapshot()["request_count"], 4)

        self.assertEqual(len(opener.calls), 4)

    def test_quota_survives_new_attempt_and_never_sends_101st(self):
        opener = RecordingOpener()
        with tempfile.TemporaryDirectory() as tmp:
            store = self.store(tmp, limit=100)
            for index in range(1, 101):
                self.score([candidate(index)], opener, store)
            first_store_count = store.snapshot()["request_count"]
            resumed = JevRunStore(store.path, calendar_date=self.DATE, request_limit=100)
            resumed.restore()
            extra = self.score([candidate(101)], opener, resumed)
            replay = self.score([candidate(50)], opener, resumed)
            self.assertEqual(first_store_count, 100)
            self.assertEqual(resumed.snapshot()["request_count"], 100)

        self.assertEqual(len(opener.calls), 100)
        self.assertEqual(extra.candidates[0]["jev_assist"]["reason"], "quota_exhausted")
        self.assertEqual(replay.candidates[0]["jev_assist"]["status"], "ok")
        self.assertTrue(replay.candidates[0]["jev_assist"]["reused"])

    def test_concurrent_claims_never_issue_the_101st_request(self):
        opener = RecordingOpener()
        with tempfile.TemporaryDirectory() as tmp:
            store = self.store(tmp, limit=100)
            barrier = threading.Barrier(20)

            def worker(index: int) -> None:
                barrier.wait()
                self.score([candidate(index)], opener, store)

            with ThreadPoolExecutor(max_workers=20) as pool:
                list(pool.map(worker, range(1, 121)))

            late = self.score([candidate(200)], opener, store)
            self.assertEqual(store.snapshot()["request_count"], 100)

        self.assertEqual(len(opener.calls), 100)
        self.assertEqual(late.candidates[0]["jev_assist"]["reason"], "quota_exhausted")

    def test_failed_request_counts_and_ambiguous_result_does_not_retry(self):
        timeout_opener = RecordingOpener([TimeoutError("timed out")])
        bad_shape = RecordingOpener([{"model": "jev-1.13.0", "answers": {}}])
        with tempfile.TemporaryDirectory() as tmp:
            store = self.store(tmp, limit=100)
            timed_out = self.score([candidate(1)], timeout_opener, store)
            again = self.score([candidate(1)], timeout_opener, store)
            invalid = self.score([candidate(2)], bad_shape, store)
            invalid_again = self.score([candidate(2)], bad_shape, store)
            self.assertEqual(store.snapshot()["request_count"], 2)

        self.assertEqual(len(timeout_opener.calls), 1)
        self.assertEqual(timed_out.candidates[0]["jev_assist"]["status"], "ambiguous")
        self.assertEqual(again.candidates[0]["jev_assist"]["status"], "ambiguous")
        self.assertTrue(again.candidates[0]["jev_assist"]["reused"])
        self.assertEqual(len(bad_shape.calls), 1)
        self.assertEqual(invalid.candidates[0]["jev_assist"]["status"], "ambiguous")
        self.assertTrue(invalid_again.candidates[0]["jev_assist"]["reused"])

    def test_service_failure_ends_the_round_without_burning_the_next_candidate(self):
        opener = RecordingOpener([http_error(429), ok_payload()])
        with tempfile.TemporaryDirectory() as tmp:
            store = self.store(tmp)
            result = self.score([candidate(1), candidate(2)], opener, store)
            self.assertEqual(store.snapshot()["request_count"], 1)

        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(result.candidates[0]["jev_assist"]["reason"], "rate_limit")
        self.assertEqual(result.candidates[1]["jev_assist"]["reason"], "rate_limit")
        self.assertEqual(result.candidates[1]["jev_assist"]["status"], "skipped")

    def test_missing_key_timeout_invalid_quota_and_disable_fail_open(self):
        opener = RecordingOpener()
        items = [candidate(1), candidate(2)]
        with tempfile.TemporaryDirectory() as tmp:
            store = self.store(tmp, limit=1)
            missing = assist_candidates(
                items,
                store=store,
                opener=opener,
                environ={},
                prior_coverage=[],
            )
            disabled = self.score(
                items, opener, store, environ={"TYPESAFE_API_KEY": "x", "JEV_ASSIST": "0"}
            )
            config_off = assist_candidates(
                items,
                config={"enabled": False},
                store=store,
                opener=opener,
                environ=self.ENV,
                prior_coverage=[],
            )
            self.score([candidate(3)], opener, store)
            exhausted = self.score([candidate(4)], opener, store)
            timeout_opener = RecordingOpener([TimeoutError("timed out")])
            timed = self.score([candidate(5)], timeout_opener, self.store(Path(tmp) / "timeout"))
            invalid_opener = RecordingOpener([{"answers": "nope"}])
            invalid = self.score([candidate(6)], invalid_opener, self.store(Path(tmp) / "invalid"))

        self.assertEqual(missing.summary["reason"], "missing_key")
        self.assertEqual(len(missing.candidates), 2)
        self.assertEqual(disabled.summary["reason"], "disabled")
        self.assertEqual(config_off.summary["reason"], "disabled")
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(exhausted.candidates[0]["jev_assist"]["reason"], "quota_exhausted")
        self.assertEqual(len(exhausted.candidates), 1)
        self.assertEqual(timed.candidates[0]["jev_assist"]["reason"], "timeout")
        self.assertEqual(invalid.candidates[0]["jev_assist"]["reason"], "invalid_response")
        self.assertFalse(any("jev_assist" in item and item["jev_assist"].get("selected") for item in items))

    def test_corrupt_store_skips_jev_without_resetting_or_calling(self):
        opener = RecordingOpener()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "quota.json"
            path.write_text("{not-json", encoding="utf-8")
            store = JevRunStore(path, calendar_date=self.DATE, request_limit=100)
            result = self.score([candidate(1)], opener, store)
            self.assertEqual(result.summary["reason"], "store_unavailable")
            self.assertEqual(len(opener.calls), 0)
            self.assertEqual(path.read_text(encoding="utf-8"), "{not-json")

    def test_in_flight_record_is_not_refired(self):
        opener = RecordingOpener()
        item = candidate(1)
        questions_id = rubric_id(build_questions({}))
        fingerprint = reuse_fingerprint(
            {**item, "prior_coverage": []},
            questions_id,
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = self.store(tmp)
            store.claim(fingerprint)
            result = self.score([item], opener, store)
            self.assertEqual(store.snapshot()["request_count"], 1)

        self.assertEqual(len(opener.calls), 0)
        self.assertEqual(result.candidates[0]["jev_assist"]["status"], "ambiguous")
        self.assertTrue(result.candidates[0]["jev_assist"]["reused"])

    def test_key_is_not_written_to_store_or_summary(self):
        opener = RecordingOpener()
        with tempfile.TemporaryDirectory() as tmp:
            store = self.store(tmp)
            result = self.score([candidate(1)], opener, store)
            dumped = store.path.read_text(encoding="utf-8") + json.dumps(result.summary)

        self.assertNotIn(self.ENV["TYPESAFE_API_KEY"], dumped)
        self.assertTrue(opener.calls[0]["has_authorization_header"])
        self.assertEqual(opener.calls[0]["body"]["model"], "jev-latest")
        self.assertIn("has_clear_new_fact", opener.calls[0]["body"]["questions"])
        self.assertIn("reader_value", opener.calls[0]["body"]["questions"])

    def test_cli_writes_assist_file_and_reuses_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidates_path = root / "candidates.json"
            out_path = root / "out.json"
            store_path = root / "runs" / "jev" / f"quota-{beijing_calendar_date()}.json"
            candidates_path.write_text(
                json.dumps({"candidates": [candidate(1)]}), encoding="utf-8"
            )
            with (
                patch(
                    "generation.jev.assist.call_jev",
                    return_value=(ok_payload(), 0.01),
                ) as mocked_call,
                patch.dict("os.environ", self.ENV, clear=False),
            ):
                jev_assist_main(
                    [
                        "--candidates",
                        str(candidates_path),
                        "--out",
                        str(out_path),
                        "--store",
                        str(store_path),
                    ]
                )
                jev_assist_main(
                    [
                        "--candidates",
                        str(candidates_path),
                        "--out",
                        str(out_path),
                        "--store",
                        str(store_path),
                    ]
                )
            payload = json.loads(out_path.read_text(encoding="utf-8"))

        self.assertEqual(mocked_call.call_count, 1)
        self.assertEqual(payload["candidates"][0]["jev_assist"]["status"], "ok")
        self.assertTrue(payload["candidates"][0]["jev_assist"]["reused"])
        self.assertTrue(payload["jev_assist"]["assist_only"])
        self.assertEqual(payload["jev_assist"]["quota_used"], 1)

    def test_parse_answers_rejects_bool_and_missing_keys(self):
        with self.assertRaises(JevClientError):
            parse_answers({"answers": {"has_clear_new_fact": {"type": "noul", "noul": True}}})
        with self.assertRaises(JevClientError):
            parse_answers({"model": "jev-1.13.0"})

    def test_print_ca_notes_mentions_fail_open_and_runtime_secret(self):
        from io import StringIO

        buffer = StringIO()
        with patch("sys.stdout", buffer):
            code = jev_assist_main(["--print-ca-notes"])
        text = buffer.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("TYPESAFE_API_KEY", text)
        self.assertIn("Runtime Secret", text)
        self.assertIn("box-secrets", text)
        self.assertIn("no_new_value", text)
        self.assertIn("JEV_ASSIST=0", text)
        self.assertIn("runs/jev", text)
        self.assertIn("AI_DAILY_PRIVATE_RUN_DIR", text)
        self.assertIn("从零", text)

    def test_client_reads_only_typesafe_env_var(self):
        source = Path("generation/jev/client.py").read_text(encoding="utf-8")
        self.assertIn("TYPESAFE_API_KEY", source)
        self.assertNotIn("box-secrets", source)
        self.assertNotIn("TYPESAFE_API_TOKEN", source)

    def test_disable_helpers(self):
        self.assertEqual(CANONICAL_DISABLE_ENV, "JEV_ASSIST")
        self.assertFalse(jev_enabled({}, {"JEV_ASSIST": "0"}))
        self.assertFalse(jev_enabled({}, {"JEV_ASSIST_DISABLED": "1"}))
        self.assertFalse(jev_enabled({"enabled": False}, {}))
        self.assertTrue(jev_enabled({}, {}))
        # Canonical JEV_ASSIST wins when set; alias is ignored.
        self.assertTrue(jev_enabled({"enabled": False}, {"JEV_ASSIST": "1"}))
        self.assertFalse(jev_enabled({"enabled": True}, {"JEV_ASSIST": "0"}))

    def test_http_success_then_result_persist_failure_fail_open(self):
        """HTTP ok this round, but writing this result fails — not a vanished prior file."""
        opener = RecordingOpener()
        with tempfile.TemporaryDirectory() as tmp:
            store = self.store(tmp)
            original_write = store._write_unlocked

            def write_fails_on_this_result(payload):
                for record in (payload.get("evaluations") or {}).values():
                    if isinstance(record, dict) and record.get("status") == "ok":
                        raise OSError(28, "No space left on device")
                return original_write(payload)

            store._write_unlocked = write_fails_on_this_result
            result = self.score([candidate(1), candidate(2)], opener, store)
            payload = json.loads(store.path.read_text(encoding="utf-8"))
            self.assertEqual(result.summary["reason"], "store_unavailable")
            self.assertEqual(result.candidates[0]["jev_assist"]["reason"], "store_unavailable")
            self.assertNotEqual(result.candidates[0]["jev_assist"]["status"], "ok")
            self.assertEqual(result.candidates[1]["jev_assist"]["reason"], "store_unavailable")
            self.assertEqual(result.candidates[1]["jev_assist"]["status"], "skipped")
            self.assertEqual(len(opener.calls), 1)
            self.assertEqual(result.summary["real_requests"], 1)
            self.assertEqual(payload["request_count"], 1)
            self.assertGreater(payload["request_count"], 0)
            in_flight = next(iter(payload["evaluations"].values()))
            self.assertTrue(in_flight["consumed_quota"])
            self.assertEqual(in_flight["status"], "in_flight")

            store._write_unlocked = original_write
            replay = self.score([candidate(1)], opener, store)
            self.assertEqual(store.snapshot()["request_count"], 1)
            self.assertEqual(len(opener.calls), 1)
            self.assertEqual(replay.summary["real_requests"], 0)
            self.assertEqual(replay.candidates[0]["jev_assist"]["status"], "ambiguous")
            self.assertTrue(replay.candidates[0]["jev_assist"]["reused"])

    def test_canonical_disable_is_checked_before_store_or_http(self):
        opener = RecordingOpener()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "quota.json"
            path.write_text("{not-json", encoding="utf-8")
            store = JevRunStore(path, calendar_date=self.DATE, request_limit=100)
            result = assist_candidates(
                [candidate(1)],
                store=store,
                opener=opener,
                environ={"TYPESAFE_API_KEY": "x", "JEV_ASSIST": "0"},
                prior_coverage=[],
            )
            self.assertEqual(result.summary["reason"], "disabled")
            self.assertEqual(len(opener.calls), 0)
            self.assertEqual(path.read_text(encoding="utf-8"), "{not-json")

    def test_disappeared_store_skips_without_resetting(self):
        opener = RecordingOpener()
        with tempfile.TemporaryDirectory() as tmp:
            store = self.store(tmp)
            self.score([candidate(1)], opener, store)
            self.assertEqual(store.snapshot()["request_count"], 1)
            store.path.unlink()
            with self.assertRaises(StoreError):
                store.snapshot()
            result = self.score([candidate(2)], opener, store)
            self.assertEqual(result.summary["reason"], "store_unavailable")
            self.assertEqual(len(opener.calls), 1)

    def test_merge_keeps_invalid_rows_for_later_selection(self):
        rows = merge_same_event(
            [
                candidate(1),
                {"title": "no evidence"},
                candidate(1, url="https://other.example/1"),
            ]
        )
        self.assertEqual(len(rows), 2)
        self.assertTrue(any(row.get("title") == "no evidence" for row in rows))


def _independent_ca_claim_batch(path_str, calendar_date, start, count, queue):
    """Standalone CA-process worker: own JevRunStore, shared durable file."""
    store = JevRunStore(
        Path(path_str),
        calendar_date=calendar_date,
        request_limit=100,
        allow_create=True,
    )
    store.restore()
    claimed = 0
    exhausted = 0
    reused = 0
    for index in range(start, start + count):
        claim = store.claim(f"cross-ca-fp-{index}")
        if claim.kind == "claimed":
            claimed += 1
        elif claim.kind == "quota_exhausted":
            exhausted += 1
        elif claim.kind == "reuse":
            reused += 1
    snapshot = store.snapshot()
    queue.put(
        {
            "claimed": claimed,
            "exhausted": exhausted,
            "reused": reused,
            "request_count": snapshot["request_count"],
        }
    )


class JevCrossCAQuotaTest(JevHelpers, unittest.TestCase):
    def test_resolve_store_location_durable_vs_workspace_resume(self):
        now = self.beijing_now()
        with tempfile.TemporaryDirectory() as tmp:
            durable = Path(tmp) / "private"
            workspace = Path(tmp) / "workspace"
            explicit = Path(tmp) / "explicit.json"
            from_private = resolve_store_location(
                now=now,
                environ={"AI_DAILY_PRIVATE_RUN_DIR": str(durable)},
                repo_root=workspace,
            )
            from_file = resolve_store_location(
                now=now,
                environ={"JEV_ASSIST_STORE": str(explicit)},
                repo_root=workspace,
            )
            from_explicit = resolve_store_location(
                now=now,
                environ={},
                explicit=explicit,
                repo_root=workspace,
            )
            from_workspace = resolve_store_location(
                now=now,
                environ={},
                repo_root=workspace,
            )
        self.assertTrue(from_private.allow_create)
        self.assertEqual(from_private.source, "durable_env")
        self.assertEqual(from_private.path, durable / "jev" / f"quota-{self.DATE}.json")
        self.assertTrue(from_file.allow_create)
        self.assertTrue(from_explicit.allow_create)
        self.assertFalse(from_workspace.allow_create)
        self.assertEqual(from_workspace.source, "workspace_resume")
        self.assertEqual(from_workspace.path, workspace / "runs" / "jev" / f"quota-{self.DATE}.json")

    def test_two_independent_cas_sharing_durable_dir_cannot_exceed_100(self):
        opener_a = RecordingOpener()
        opener_b = RecordingOpener()
        with tempfile.TemporaryDirectory() as tmp:
            durable = Path(tmp) / "shared-private"
            ca_a_root = Path(tmp) / "ca-a"
            ca_b_root = Path(tmp) / "ca-b"
            first = self.score_shared_dir(
                [candidate(index) for index in range(1, 61)],
                opener_a,
                durable,
                ca_a_root,
            )
            second = self.score_shared_dir(
                [candidate(index) for index in range(61, 121)],
                opener_b,
                durable,
                ca_b_root,
            )
            quota_path = durable / "jev" / f"quota-{self.DATE}.json"
            payload = json.loads(quota_path.read_text(encoding="utf-8"))
            late = self.score_shared_dir([candidate(200)], RecordingOpener(), durable, ca_b_root)

        self.assertEqual(len(opener_a.calls), 60)
        self.assertEqual(len(opener_b.calls), 40)
        self.assertEqual(first.summary["real_requests"], 60)
        self.assertEqual(second.summary["real_requests"], 40)
        self.assertEqual(payload["request_count"], 100)
        self.assertLessEqual(len(opener_a.calls) + len(opener_b.calls), 100)
        self.assertEqual(late.candidates[0]["jev_assist"]["reason"], "quota_exhausted")
        self.assertEqual(late.summary["real_requests"], 0)

    def test_two_os_processes_sharing_durable_file_stay_at_100(self):
        with tempfile.TemporaryDirectory() as tmp:
            durable = Path(tmp) / "shared-private" / "jev"
            durable.mkdir(parents=True)
            path = durable / f"quota-{self.DATE}.json"
            queue: Queue = Queue()
            workers = [
                Process(
                    target=_independent_ca_claim_batch,
                    args=(str(path), self.DATE, 1, 80, queue),
                ),
                Process(
                    target=_independent_ca_claim_batch,
                    args=(str(path), self.DATE, 81, 80, queue),
                ),
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=30)
                if worker.is_alive():
                    worker.terminate()
                    worker.join(timeout=5)
                    self.fail("independent CA worker hung on the shared store")
                self.assertEqual(worker.exitcode, 0)
            results = [queue.get(timeout=5), queue.get(timeout=5)]
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(sum(item["claimed"] for item in results), 100)
        self.assertEqual(sum(item["exhausted"] for item in results), 60)
        self.assertEqual(payload["request_count"], 100)
        self.assertEqual(len(payload["evaluations"]), 100)

    def test_third_ca_missing_workspace_store_skips_without_http_or_reset(self):
        opener_shared = RecordingOpener()
        opener_fresh = RecordingOpener()
        with tempfile.TemporaryDirectory() as tmp:
            durable = Path(tmp) / "shared-private"
            self.score_shared_dir(
                [candidate(index) for index in range(1, 6)],
                opener_shared,
                durable,
                Path(tmp) / "ca-shared",
            )
            shared_count = json.loads(
                (durable / "jev" / f"quota-{self.DATE}.json").read_text(encoding="utf-8")
            )["request_count"]
            fresh_vm = Path(tmp) / "fresh-vm"
            result = assist_candidates(
                [candidate(99)],
                opener=opener_fresh,
                environ=self.ENV,
                repo_root=fresh_vm,
                now=self.beijing_now(),
                prior_coverage=[],
            )
            created = list(fresh_vm.rglob("quota-*.json")) if fresh_vm.exists() else []

        self.assertEqual(shared_count, 5)
        self.assertTrue(result.skipped)
        self.assertEqual(result.summary["reason"], "store_unavailable")
        self.assertIn("missing", result.summary["store_error"])
        self.assertEqual(len(opener_fresh.calls), 0)
        self.assertEqual(created, [])

    def test_third_ca_corrupt_or_empty_shared_store_skips_without_reset(self):
        opener = RecordingOpener()
        with tempfile.TemporaryDirectory() as tmp:
            durable = Path(tmp) / "shared-private"
            self.score_shared_dir([candidate(1)], RecordingOpener(), durable, Path(tmp) / "ca-1")
            quota_path = durable / "jev" / f"quota-{self.DATE}.json"
            quota_path.write_text("{not-json", encoding="utf-8")
            corrupt = self.score_shared_dir(
                [candidate(2)], opener, durable, Path(tmp) / "ca-corrupt"
            )
            self.assertEqual(quota_path.read_text(encoding="utf-8"), "{not-json")
            quota_path.write_text("   \n", encoding="utf-8")
            empty = self.score_shared_dir(
                [candidate(3)], opener, durable, Path(tmp) / "ca-empty"
            )
            self.assertEqual(quota_path.read_text(encoding="utf-8").strip(), "")
            quota_path.write_text("[]", encoding="utf-8")
            wrong_shape = self.score_shared_dir(
                [candidate(4)], opener, durable, Path(tmp) / "ca-shape"
            )
            self.assertEqual(quota_path.read_text(encoding="utf-8"), "[]")

        self.assertEqual(len(opener.calls), 0)
        self.assertEqual(corrupt.summary["reason"], "store_unavailable")
        self.assertEqual(empty.summary["reason"], "store_unavailable")
        self.assertEqual(wrong_shape.summary["reason"], "store_unavailable")
        self.assertIn("parsed", corrupt.summary["store_error"])
        self.assertIn("empty", empty.summary["store_error"])
        self.assertIn("not an object", wrong_shape.summary["store_error"])

    def test_leftover_lock_without_json_does_not_mint_a_new_100(self):
        opener = RecordingOpener()
        with tempfile.TemporaryDirectory() as tmp:
            durable = Path(tmp) / "shared-private"
            jev_dir = durable / "jev"
            jev_dir.mkdir(parents=True)
            lock = jev_dir / f"quota-{self.DATE}.json.lock"
            lock.write_text("held\n", encoding="utf-8")
            result = self.score_shared_dir(
                [candidate(1)], opener, durable, Path(tmp) / "ca-lock"
            )
            quota_path = jev_dir / f"quota-{self.DATE}.json"
            self.assertFalse(quota_path.exists())
            self.assertEqual(lock.read_text(encoding="utf-8"), "held\n")

        self.assertEqual(result.summary["reason"], "store_unavailable")
        self.assertEqual(len(opener.calls), 0)

    def test_permission_and_lock_failures_skip_without_http(self):
        opener = RecordingOpener()
        with tempfile.TemporaryDirectory() as tmp:
            durable = Path(tmp) / "shared-private"
            seeded = self.score_shared_dir(
                [candidate(1)], RecordingOpener(), durable, Path(tmp) / "ca-seed"
            )
            quota_path = durable / "jev" / f"quota-{self.DATE}.json"
            self.assertEqual(seeded.summary["real_requests"], 1)
            quota_path.chmod(0o000)
            try:
                denied = self.score_shared_dir(
                    [candidate(2)], opener, durable, Path(tmp) / "ca-denied"
                )
            finally:
                quota_path.chmod(0o644)
            parent_is_file = Path(tmp) / "not-a-directory"
            parent_is_file.write_text("blocker", encoding="utf-8")
            blocked = assist_candidates(
                [candidate(3)],
                opener=opener,
                environ={**self.ENV, "AI_DAILY_PRIVATE_RUN_DIR": str(parent_is_file)},
                repo_root=Path(tmp) / "ca-blocked",
                now=self.beijing_now(),
                prior_coverage=[],
            )
            with patch("generation.jev.store.fcntl.flock", side_effect=OSError("flock failed")):
                locked = self.score_shared_dir(
                    [candidate(4)], opener, durable, Path(tmp) / "ca-flock"
                )

        self.assertEqual(len(opener.calls), 0)
        self.assertEqual(denied.summary["reason"], "store_unavailable")
        self.assertEqual(blocked.summary["reason"], "store_unavailable")
        self.assertEqual(locked.summary["reason"], "store_unavailable")
        self.assertIn("cannot be read", denied.summary["store_error"])
        self.assertIn("cannot be locked", blocked.summary["store_error"])
        self.assertIn("cannot be locked", locked.summary["store_error"])

    def test_workspace_resume_does_not_create_and_durable_first_day_does(self):
        opener_workspace = RecordingOpener()
        opener_durable = RecordingOpener()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "repo"
            missing = assist_candidates(
                [candidate(1)],
                opener=opener_workspace,
                environ=self.ENV,
                repo_root=workspace,
                now=self.beijing_now(),
                prior_coverage=[],
            )
            self.assertFalse(any(workspace.rglob("quota-*.json")))
            durable = Path(tmp) / "private"
            created = self.score_shared_dir(
                [candidate(1)], opener_durable, durable, Path(tmp) / "ca-first"
            )
            quota_path = durable / "jev" / f"quota-{self.DATE}.json"
            self.assertTrue(quota_path.exists())
            self.assertEqual(
                json.loads(quota_path.read_text(encoding="utf-8"))["request_count"],
                1,
            )

        self.assertEqual(missing.summary["reason"], "store_unavailable")
        self.assertEqual(len(opener_workspace.calls), 0)
        self.assertEqual(len(opener_durable.calls), 1)
        self.assertEqual(created.summary["real_requests"], 1)

    def test_concurrent_writers_on_two_store_handles_stay_at_100(self):
        opener = RecordingOpener()
        with tempfile.TemporaryDirectory() as tmp:
            durable = Path(tmp) / "shared-private" / "jev"
            durable.mkdir(parents=True)
            path = durable / f"quota-{self.DATE}.json"
            store_a = JevRunStore(path, calendar_date=self.DATE, request_limit=100, allow_create=True)
            store_b = JevRunStore(path, calendar_date=self.DATE, request_limit=100, allow_create=True)
            store_a.restore()
            store_b.restore()

            def worker(handle, index):
                self.score([candidate(index)], opener, handle)

            with ThreadPoolExecutor(max_workers=16) as pool:
                futures = []
                for index in range(1, 121):
                    handle = store_a if index % 2 == 0 else store_b
                    futures.append(pool.submit(worker, handle, index))
                for future in futures:
                    future.result()
            self.assertEqual(store_a.snapshot()["request_count"], 100)
            self.assertEqual(store_b.snapshot()["request_count"], 100)

        self.assertEqual(len(opener.calls), 100)


if __name__ == "__main__":
    unittest.main()
