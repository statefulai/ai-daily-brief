import unittest

from source_status import run_source
from sources import NewsItem, SourceFetchError


class SourceStatusTest(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_source_is_skipped(self):
        result = await run_source("rss", "RSS", lambda config: None, {"enabled": False})
        self.assertEqual(result.status, "skipped")
        self.assertEqual(result.items, [])

    async def test_empty_success_is_no_candidates(self):
        async def fetch(_config):
            return []

        result = await run_source("rss", "RSS", fetch, {"enabled": True})
        self.assertEqual(result.status, "no_candidates")

    async def test_exception_is_failed(self):
        async def fetch(_config):
            raise SourceFetchError("HTTP 503")

        result = await run_source("rss", "RSS", fetch, {"enabled": True})
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error, "HTTP 503")

    async def test_items_are_success(self):
        async def fetch(_config):
            return [NewsItem(title="A", url="https://example.com/a", source="RSS")]

        result = await run_source("rss", "RSS", fetch, {"enabled": True})
        self.assertEqual(result.status, "success")
        self.assertEqual(result.to_record()["item_count"], 1)
