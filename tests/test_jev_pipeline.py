import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from curate import assemble_curated_events
from edition import build_edition, decide_generation_status
from scripts.edition_ci import render_edition_file
from sources import NewsItem
from summarizer import curate_daily_brief
from verify import verify_event_primary_sources, verify_primary_source

try:
    from test_edition import sample_event, sample_source
    from test_jev_assist import JevHelpers, RecordingOpener, candidate, ok_payload
except ImportError:  # python -m unittest tests.test_jev_pipeline
    from tests.test_edition import sample_event, sample_source
    from tests.test_jev_assist import JevHelpers, RecordingOpener, candidate, ok_payload


class JevPipelineTest(JevHelpers, unittest.TestCase):
    def test_low_scores_do_not_force_empty_edition(self):
        opener = RecordingOpener([ok_payload(noul=0.02, score=0.1)])
        with tempfile.TemporaryDirectory() as tmp:
            store = self.store(tmp)
            result = self.score([candidate(1)], opener, store)

        self.assertEqual(result.candidates[0]["jev_assist"]["answers"]["has_clear_new_fact"]["noul"], 0.02)
        self.assertEqual(len(result.candidates), 1)
        self.assertNotEqual(result.summary.get("reason"), "no_new_value")
        self.assertEqual(
            decide_generation_status([sample_source()], [sample_event()]),
            "published_candidate",
        )
        self.assertEqual(
            decide_generation_status([sample_source("no_candidates", 0)], []),
            "no_new_value",
        )

    def test_high_scores_cannot_bypass_source_verification(self):
        opener = RecordingOpener([ok_payload(noul=0.99, score=3.0)])
        with tempfile.TemporaryDirectory() as tmp:
            store = self.store(tmp)
            result = self.score([candidate(1)], opener, store)
            event = sample_event()
            event["sources"][0]["verified"] = False
            event["jev_assist"] = result.candidates[0]["jev_assist"]
            document = build_edition(
                edition_id="2026-09-16",
                cutoff_at="2026-09-16T08:00:00+08:00",
                sources=[sample_source()],
                events=[event],
            )
            document["events"][0]["sources"][0]["verified"] = False
            document.pop("content_hash", None)
            editions = Path(tmp) / "editions"
            target = editions / document["edition_id"]
            target.mkdir(parents=True)
            edition_path = target / "edition.json"
            edition_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n")

            with self.assertRaisesRegex(Exception, "no verified primary source"):
                render_edition_file(edition_path)

        self.assertFalse(result.candidates[0].get("verified"))
        self.assertIsNone(
            (result.candidates[0].get("sources") or [{}])[0].get("verified")
            if result.candidates[0].get("sources")
            else None
        )
        html = '{"datePublished":"2026-09-15T17:00:00+00:00"}'
        checked = verify_primary_source("https://example.com/a", html)
        self.assertTrue(checked["verified"])
        failed = verify_primary_source("https://example.com/a", None, fetch_error="HTTP 503")
        self.assertFalse(failed["verified"])

    def test_assist_never_marks_primary_source_verified(self):
        opener = RecordingOpener([ok_payload(noul=0.99, score=3.0)])
        item = candidate(1)
        item["sources"] = [
            {
                "label": "Official",
                "url": "https://example.com/1",
                "kind": "primary",
                "verified": False,
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            result = self.score([item], opener, self.store(tmp))

        self.assertFalse(result.candidates[0]["sources"][0]["verified"])
        self.assertNotIn("verified", result.candidates[0]["jev_assist"])

    def test_verify_pipeline_still_fetches_after_high_jev_score(self):
        class Response:
            status_code = 503
            text = ""

        class Client:
            async def get(self, url, headers):
                self.url = url
                return Response()

        opener = RecordingOpener([ok_payload(noul=0.99, score=3.0)])
        with tempfile.TemporaryDirectory() as tmp:
            scored = self.score([candidate(1)], opener, self.store(tmp))
        event = sample_event()
        event["sources"][0]["verified"] = False
        event["jev_assist"] = scored.candidates[0]["jev_assist"]
        client = Client()
        updated = asyncio.run(verify_event_primary_sources([event], client=client))
        self.assertEqual(client.url, "https://example.com/a")
        self.assertFalse(updated[0]["sources"][0]["verified"])
        self.assertEqual(updated[0]["sources"][0]["note"], "HTTP 503")

    def test_curate_keeps_every_clustered_candidate_after_low_jev_scores(self):
        scored = [
            {
                "title": "News A",
                "url": "https://example.com/a",
                "source": "Test",
                "score": 10,
                "published": datetime.now(timezone.utc).isoformat(),
                "summary": "Official released capability A",
                "category": "product",
                "importance": 8,
                "topic_key": "topic-a",
                "tags": [],
            },
            {
                "title": "News B",
                "url": "https://example.com/b",
                "source": "Test",
                "score": 9,
                "published": datetime.now(timezone.utc).isoformat(),
                "summary": "Official released capability B",
                "category": "product",
                "importance": 7,
                "topic_key": "topic-b",
                "tags": [],
            },
        ]
        brief = {
            "focus": {
                "index": 0,
                "title_zh": "模型选择发生变化",
                "editorial": "要点：新模型降低了推理延迟；影响：开发团队可重新评估生产选型。",
            },
            "highlights": [
                {
                    "index": 1,
                    "title_zh": "第二条仍在",
                    "editorial": "要点：工具更新了权限控制；影响：团队可减少人工配置步骤。",
                }
            ],
            "tools": [],
        }
        items = [
            NewsItem(title="News A", url="https://example.com/a", source="Test"),
            NewsItem(title="News B", url="https://example.com/b", source="Test"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            store = self.store(tmp)
            with (
                patch("summarizer._run_stage1", return_value=scored),
                patch("summarizer._run_stage2", return_value=brief) as stage2,
                patch(
                    "generation.jev.assist.call_jev",
                    return_value=(ok_payload(noul=0.01, score=0.2), 0.01),
                ),
                self.patch_assist_calendar(),
                patch.dict("os.environ", self.ENV, clear=False),
            ):
                result = curate_daily_brief(
                    items,
                    {},
                    jev={
                        "store_path": str(store.path),
                        "repo_root": tmp,
                    },
                )

        self.assertEqual(result["brief"], brief)
        passed = stage2.call_args[0][0]
        self.assertEqual(len(passed), 2)
        self.assertEqual(passed[0]["jev_assist"]["answers"]["reader_value"]["score"], 0.2)
        self.assertEqual(passed[1]["jev_assist"]["answers"]["reader_value"]["score"], 0.2)
        events = assemble_curated_events(result)
        self.assertEqual(len(events), 2)

    def test_empty_edition_still_requires_existing_zero_selection_rules(self):
        opener = RecordingOpener([ok_payload(noul=0.01, score=0.0)])
        with tempfile.TemporaryDirectory() as tmp:
            result = self.score([candidate(1)], opener, self.store(tmp))
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(
            decide_generation_status([sample_source("failed", 0, "timeout")], []),
            "failed",
        )
        self.assertNotEqual(result.summary.get("reason"), "no_new_value")
        self.assertNotEqual(result.summary.get("reason"), "failed")


if __name__ == "__main__":
    unittest.main()
