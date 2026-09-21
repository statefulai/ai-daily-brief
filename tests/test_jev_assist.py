import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
import urllib.error

from generation.jev.assist import assist_candidates, jev_enabled
from generation.jev.client import JevClientError, parse_answers
from generation.jev.fingerprint import reuse_fingerprint, rubric_id
from generation.jev.merge import merge_same_event
from generation.jev.rubric import build_questions
from generation.jev.store import JevRunStore, StoreError, beijing_calendar_date
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

    def store(self, directory, *, limit=100):
        path = Path(directory) / "runs" / "jev" / f"quota-{self.DATE}.json"
        store = JevRunStore(path, calendar_date=self.DATE, request_limit=limit)
        store.restore()
        return store

    def score(self, items, opener, store, **kwargs):
        return assist_candidates(
            items,
            store=store,
            opener=opener,
            environ=kwargs.pop("environ", self.ENV),
            prior_coverage=kwargs.pop("prior_coverage", []),
            **kwargs,
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

    def test_client_reads_only_typesafe_env_var(self):
        source = Path("generation/jev/client.py").read_text(encoding="utf-8")
        self.assertIn("TYPESAFE_API_KEY", source)
        self.assertNotIn("box-secrets", source)
        self.assertNotIn("TYPESAFE_API_TOKEN", source)

    def test_disable_helpers(self):
        self.assertFalse(jev_enabled({}, {"JEV_ASSIST": "0"}))
        self.assertFalse(jev_enabled({}, {"JEV_ASSIST_DISABLED": "1"}))
        self.assertFalse(jev_enabled({"enabled": False}, {}))
        self.assertTrue(jev_enabled({}, {}))

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


if __name__ == "__main__":
    unittest.main()
