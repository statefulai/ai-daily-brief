import asyncio
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from edition import (
    EditionError,
    acquire_edition_lock,
    build_edition,
    content_hash,
    decide_generation_status,
    edition_id_for,
    edition_window,
    merge_events,
    next_attempt,
    release_edition_lock,
    validate_edition,
    write_edition,
)
from contributions import (
    event_overrides,
    filter_contributions_by_window,
    validate_contribution,
)
from verify import extract_published_at, verify_event_primary_sources, verify_primary_source
from curate import assemble_events
from sources import NewsItem


def sample_event(event_id: str = "voice", url: str = "https://example.com/a") -> dict:
    return {
        "id": event_id,
        "placement": "lead",
        "window": "in_window",
        "title": "模型发布可并行调用工具",
        "kicker": "焦点",
        "facts": ["官方发布了可在语音中调用工具的模型。"],
        "conditions": ["未实测延迟。"],
        "background": ["可选背景。"],
        "sources": [
            {
                "label": "官方公告",
                "url": url,
                "kind": "primary",
                "published_at": "2026-09-15T17:00:00+00:00",
                "verified": True,
                "note": None,
            }
        ],
    }


def sample_source(status: str = "success", item_count: int = 1, error: str | None = None) -> dict:
    return {"id": "hackernews", "status": status, "item_count": item_count, "error": error}


class EditionContractTest(unittest.TestCase):
    def test_daily_window_uses_latest_closed_beijing_0800_boundary(self):
        beijing = timezone(timedelta(hours=8))

        start, cutoff = edition_window(datetime(2026, 9, 17, 8, 0, tzinfo=beijing))
        self.assertEqual(start.isoformat(), "2026-09-16T08:00:00+08:00")
        self.assertEqual(cutoff.isoformat(), "2026-09-17T08:00:00+08:00")
        self.assertEqual(edition_id_for(cutoff), "2026-09-17")

        early_start, early_cutoff = edition_window(
            datetime(2026, 9, 17, 7, 59, tzinfo=beijing)
        )
        self.assertEqual(early_start.isoformat(), "2026-09-15T08:00:00+08:00")
        self.assertEqual(early_cutoff.isoformat(), "2026-09-16T08:00:00+08:00")
        self.assertEqual(edition_id_for(datetime(2026, 9, 17, 7, 59, tzinfo=beijing)), "2026-09-16")

    def test_local_edition_excludes_platform_runner_identity(self):
        document = build_edition(
            edition_id="2026-09-16",
            cutoff_at="2026-09-16T08:00:00+08:00",
            sources=[sample_source()],
            events=[sample_event()],
            generated_at="2026-09-16T08:01:00+08:00",
        )

        self.assertNotIn("cloud_agent_name", document)

    def test_content_hash_ignores_attempt_and_generated_at(self):
        base = {
            "schema_version": "1",
            "edition_id": "2026-09-16",
            "timezone": "Asia/Shanghai",
            "cutoff_at": "2026-09-16T08:00:00+08:00",
            "generated_at": "2026-09-16T08:01:00+08:00",
            "attempt": 1,
            "status": "published_candidate",
            "sources": [sample_source()],
            "events": [sample_event()],
            "contribution_count": 0,
            "failure": None,
        }
        first = content_hash(base)
        base["attempt"] = 2
        base["generated_at"] = "2026-09-16T09:00:00+08:00"
        self.assertEqual(first, content_hash(base))

    def test_no_new_value_cannot_hide_source_failure(self):
        with self.assertRaisesRegex(EditionError, "source failure"):
            validate_edition(
                {
                    "schema_version": "1",
                    "edition_id": "2026-09-16",
                    "timezone": "Asia/Shanghai",
                    "cutoff_at": "2026-09-16T08:00:00+08:00",
                    "status": "no_new_value",
                    "sources": [sample_source("failed", 0, "timeout")],
                    "events": [],
                    "contribution_count": 0,
                    "failure": None,
                }
            )

    def test_decide_status_distinguishes_failure_and_empty_day(self):
        self.assertEqual(
            decide_generation_status([sample_source("failed", 0, "timeout")], []),
            "failed",
        )
        self.assertEqual(
            decide_generation_status([sample_source("no_candidates", 0)], []),
            "no_new_value",
        )
        self.assertEqual(
            decide_generation_status([sample_source()], [sample_event()]),
            "published_candidate",
        )

    def test_same_date_retry_increments_attempt_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            editions = Path(tmp)
            first = {
                "schema_version": "1",
                "edition_id": "2026-09-16",
                "timezone": "Asia/Shanghai",
                "cutoff_at": "2026-09-16T08:00:00+08:00",
                "generated_at": "2026-09-16T08:01:00+08:00",
                "attempt": 1,
                "status": "published_candidate",
                "sources": [sample_source()],
                "events": [sample_event()],
                "contribution_count": 0,
            }
            write_edition(editions, first)
            self.assertEqual(next_attempt(editions, "2026-09-16"), 2)

    def test_mutex_rejects_second_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            editions = Path(tmp)
            lock = acquire_edition_lock(editions, "2026-09-16")
            with self.assertRaisesRegex(EditionError, "already running"):
                acquire_edition_lock(editions, "2026-09-16")
            release_edition_lock(lock)
            second = acquire_edition_lock(editions, "2026-09-16")
            release_edition_lock(second)

    def test_mutex_has_one_winner_under_concurrency(self):
        with tempfile.TemporaryDirectory() as tmp:
            editions = Path(tmp)
            ready = threading.Barrier(8)
            attempted = threading.Barrier(8)

            def contender() -> bool:
                ready.wait()
                try:
                    lock = acquire_edition_lock(editions, "2026-09-16")
                except EditionError:
                    attempted.wait()
                    return False
                # Keep the winning lock until every contender has attempted
                # acquisition, so later threads cannot become sequential winners.
                attempted.wait()
                release_edition_lock(lock)
                return True

            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(contender) for _ in range(8)]
                winners = sum(future.result() for future in futures)

            self.assertEqual(winners, 1)

    def test_merge_events_keeps_variable_count(self):
        merged = merge_events(
            [
                sample_event("one", "https://example.com/a"),
                sample_event("two", "https://example.com/a"),
                sample_event("three", "https://example.com/b"),
            ]
        )
        self.assertEqual(len(merged), 2)

    def test_write_skips_non_published_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(EditionError, "published_candidate"):
                write_edition(
                    Path(tmp),
                    {
                        "schema_version": "1",
                        "edition_id": "2026-09-16",
                        "timezone": "Asia/Shanghai",
                        "cutoff_at": "2026-09-16T08:00:00+08:00",
                        "status": "no_new_value",
                        "sources": [sample_source("no_candidates", 0)],
                        "events": [],
                    },
                )


class IntelAndVerifyTest(unittest.TestCase):
    def test_contribution_requires_fresh_primary_source_verification(self):
        record = {
            "event": "模型发布",
            "primary_source": {
                "title": "官方公告",
                "url": "https://example.com/announcement",
            },
            "source_time": "2026-09-16T01:00:00+08:00",
            "verified_facts": ["供稿侧已核对该发布。"],
            "conditions": [],
            "sensitivity": "public",
        }

        source = event_overrides([record])[
            "https://example.com/announcement"
        ]["sources"][0]

        self.assertFalse(source["verified"])
        self.assertIn("重新抓取一手来源", source["note"])

    def test_contributions_share_the_same_half_open_daily_window(self):
        beijing = timezone(timedelta(hours=8))
        start = datetime(2026, 9, 16, 8, 0, tzinfo=beijing)
        cutoff = datetime(2026, 9, 17, 8, 0, tzinfo=beijing)

        def record(source_time: str, *, sensitivity: str = "public") -> dict:
            return {
                "event": source_time,
                "primary_source": {
                    "title": "公告",
                    "url": f"https://example.com/{source_time}",
                },
                "source_time": source_time,
                "verified_facts": ["已核对。"],
                "conditions": [],
                "sensitivity": sensitivity,
            }

        records = [
            record("2026-09-16T07:59:59+08:00"),
            record("2026-09-16T00:00:00+00:00"),
            record("2026-09-16T12:00:00+00:00", sensitivity="internal"),
            record("2026-09-17T07:59:59+08:00"),
            record("2026-09-17T08:00:00+08:00"),
        ]

        selected = filter_contributions_by_window(records, start, cutoff)

        self.assertEqual(
            [item["source_time"] for item in selected],
            ["2026-09-16T00:00:00+00:00", "2026-09-17T07:59:59+08:00"],
        )

    def test_intel_requires_verified_facts(self):
        with self.assertRaisesRegex(EditionError, "verified_facts"):
            validate_contribution(
                {
                    "event": "模型发布",
                    "primary_source": {"title": "公告", "url": "https://example.com/a"},
                    "source_time": "2026-09-16T01:00:00+08:00",
                    "verified_facts": [],
                    "conditions": [],
                    "sensitivity": "public",
                }
            )

    def test_primary_source_fetch_failure_is_not_verified(self):
        result = verify_primary_source(
            "https://example.com/a",
            None,
            fetch_error="HTTP 503",
        )
        self.assertFalse(result["verified"])
        self.assertEqual(result["reason"], "HTTP 503")

    def test_extracts_json_ld_published_time(self):
        html = '{"datePublished":"2026-09-15T17:00:00+00:00"}'
        self.assertEqual(extract_published_at(html), "2026-09-15T17:00:00+00:00")
        self.assertTrue(verify_primary_source("https://example.com/a", html)["verified"])

    def test_source_time_without_timezone_is_not_marked_verified(self):
        result = verify_primary_source(
            "https://example.com/a",
            '{"datePublished":"2026-09-15"}',
        )

        self.assertFalse(result["verified"])
        self.assertEqual(result["reason"], "published time in primary source metadata has no timezone")

    def test_selected_event_primary_page_is_verified_in_pipeline(self):
        class Response:
            status_code = 200
            text = '{"datePublished":"2026-09-15T17:00:00+00:00"}'

        class Client:
            async def get(self, url, headers):
                self.url = url
                return Response()

        event = sample_event()
        event["sources"][0]["verified"] = False
        event["sources"][0]["published_at"] = None
        client = Client()
        verified = asyncio.run(verify_event_primary_sources([event], client=client))

        self.assertEqual(client.url, "https://example.com/a")
        self.assertTrue(verified[0]["sources"][0]["verified"])
        self.assertEqual(
            verified[0]["sources"][0]["published_at"],
            "2026-09-15T17:00:00+00:00",
        )

    def test_primary_page_failure_stays_visible_on_event(self):
        class Response:
            status_code = 503
            text = ""

        class Client:
            async def get(self, url, headers):
                return Response()

        event = sample_event()
        event["sources"][0]["verified"] = False
        verified = asyncio.run(verify_event_primary_sources([event], client=Client()))
        source = verified[0]["sources"][0]

        self.assertFalse(source["verified"])
        self.assertEqual(source["note"], "HTTP 503")

    def test_assemble_events_has_no_fixed_count(self):
        items = [
            NewsItem(title="A", url="https://example.com/a", source="Test", summary="事实A", published=datetime.now(timezone.utc)),
            NewsItem(title="B", url="https://example.com/b", source="Test", summary="事实B", published=datetime.now(timezone.utc)),
            NewsItem(title="A copy", url="https://example.com/a", source="RSS", summary="补充", published=datetime.now(timezone.utc)),
        ]
        events = assemble_events(items)
        self.assertEqual(len(events), 2)
        self.assertIn("补充", events[0]["facts"])

    def test_event_source_rejects_non_http_url(self):
        event = sample_event()
        event["sources"][0]["url"] = "javascript:alert(1)"
        with self.assertRaisesRegex(EditionError, "absolute http"):
            validate_edition(
                {
                    "schema_version": "1",
                    "edition_id": "2026-09-16",
                    "timezone": "Asia/Shanghai",
                    "cutoff_at": "2026-09-16T08:00:00+08:00",
                    "status": "published_candidate",
                    "sources": [sample_source()],
                    "events": [event],
                    "contribution_count": 0,
                }
            )


if __name__ == "__main__":
    unittest.main()
