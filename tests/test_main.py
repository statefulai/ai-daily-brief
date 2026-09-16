import unittest
from unittest.mock import AsyncMock, patch

from main import write_legacy_outputs


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
