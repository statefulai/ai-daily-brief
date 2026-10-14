import argparse
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from main import filter_by_window, load_contributions, run, write_legacy_outputs
from source_health import observe_source_health
from source_status import SourceResult
from sources import NewsItem


class DailyInputWindowTest(unittest.TestCase):
    def test_public_items_use_the_same_half_open_daily_window(self):
        beijing = timezone(timedelta(hours=8))
        start = datetime(2026, 9, 16, 8, 0, tzinfo=beijing)
        cutoff = datetime(2026, 9, 17, 8, 0, tzinfo=beijing)

        def item(label: str, published: datetime) -> NewsItem:
            return NewsItem(
                title=label,
                url=f"https://example.com/{label}",
                source="Test",
                published=published,
            )

        selected = filter_by_window(
            [
                item("before", start - timedelta(seconds=1)),
                item("start", start.astimezone(timezone.utc)),
                item("inside", cutoff - timedelta(seconds=1)),
                item("cutoff", cutoff),
            ],
            start,
            cutoff,
        )

        self.assertEqual([entry.title for entry in selected], ["start", "inside"])

    def test_omitted_contribution_file_means_no_contribution(self):
        self.assertEqual(load_contributions(None), [])

    def test_explicit_missing_contribution_file_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.json"

            with self.assertRaisesRegex(FileNotFoundError, "contribution file not found"):
                load_contributions(str(missing))

    def test_invalid_contribution_file_still_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            path.write_text(json.dumps({"items": [{"event": "missing fields"}]}))

            with self.assertRaisesRegex(ValueError, "primary_source"):
                load_contributions(str(path))


class LegacyMigrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_no_valid_brief_preserves_existing_legacy_outputs(self):
        with (
            patch("main.write_readme") as write_readme,
            patch("main.write_archive") as write_archive,
            patch("main.push_to_notion", new_callable=AsyncMock) as push_to_notion,
        ):
            count = await write_legacy_outputs(
                {"candidates": [], "brief": {}},
                {
                    "github_readme": {"enabled": True},
                    "archive": {"enabled": True},
                },
            )

        self.assertEqual(count, 0)
        write_readme.assert_not_called()
        write_archive.assert_not_called()
        push_to_notion.assert_not_awaited()

    async def test_valid_brief_keeps_legacy_outputs_running(self):
        result = {
            "candidates": [{"title": "A"}],
            "brief": {"focus": {"index": 0}, "highlights": [], "tools": []},
        }
        with (
            patch("main.format_daily_brief", return_value="brief") as format_brief,
            patch("main.write_readme") as write_readme,
            patch("main.write_archive") as write_archive,
            patch("main.push_to_notion", new_callable=AsyncMock) as push_to_notion,
        ):
            count = await write_legacy_outputs(
                result,
                {
                    "github_readme": {
                        "enabled": True,
                        "file": "daily-brief.md",
                    },
                    "archive": {"enabled": True},
                },
            )

        self.assertEqual(count, 1)
        format_brief.assert_called_once()
        write_readme.assert_called_once_with("brief", "daily-brief.md")
        write_archive.assert_called_once()
        push_to_notion.assert_awaited_once()


class SourceHealthWiringTest(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_previews_the_alert_and_a_real_run_writes_it(self):
        cutoff = datetime(2026, 10, 14, 8, 0, tzinfo=timezone(timedelta(hours=8)))
        failed = [SourceResult("mistral", "Mistral", "failed", error="mistral.ai: HTTP 404")]
        text = "来源告警：Mistral（mistral）已连续 3 天抓取失败，自 2026-10-12 起。原因：mistral.ai: HTTP 404"
        args = argparse.Namespace(
            config="config.yaml", contributions=None, sources_only=True, dry_run=True
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source_health"
            observe_source_health("2026-10-12", failed, persist=True, root=root)
            observe_source_health("2026-10-13", failed, persist=True, root=root)
            with (
                patch.dict(os.environ, {"AI_DAILY_PRIVATE_RUN_DIR": directory}),
                patch("main.edition_window", return_value=(cutoff - timedelta(days=1), cutoff)),
                patch("main.fetch_all_source_results", new_callable=AsyncMock, return_value=failed),
                redirect_stdout(io.StringIO()),
            ):
                with self.assertLogs("ai-news", level="WARNING") as dry_run_logs:
                    await run(args)
                self.assertIn(f"WARNING:ai-news:{text}", dry_run_logs.output)
                self.assertFalse((root / "alerts").exists(), "dry run writes no alert")
                self.assertFalse((root / "days" / "2026-10-14").exists(), "dry run writes no day")

                args.dry_run = False
                await run(args)

            alert = json.loads(
                (root / "alerts" / "2026-10-14" / "mistral.1.json").read_text(encoding="utf-8")
            )
            self.assertEqual((alert["alert_id"], alert["text"]), ("2026-10-14:mistral:1", text))


if __name__ == "__main__":
    unittest.main()
