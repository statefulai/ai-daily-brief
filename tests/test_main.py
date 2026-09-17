import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from main import filter_by_window, load_grok_contributions, write_legacy_outputs
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

    def test_absent_optional_contribution_file_means_no_contribution(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.json"
            config = {"integrations": {"grok_bot": {"contributions_path": str(missing)}}}

            self.assertEqual(load_grok_contributions(config), [])

    def test_invalid_contribution_file_still_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            path.write_text(json.dumps({"items": [{"event": "missing fields"}]}))
            config = {"integrations": {"grok_bot": {"contributions_path": str(path)}}}

            with self.assertRaisesRegex(ValueError, "primary_source"):
                load_grok_contributions(config)


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


if __name__ == "__main__":
    unittest.main()
