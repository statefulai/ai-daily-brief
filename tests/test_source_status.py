import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx
import yaml

from source_status import SourceConfigError, SourceSpec, parse_source_specs, run_source
from sources import (
    NewsItem,
    SourceFetchError,
    fetch_deepseek_updates,
    fetch_feed,
    fetch_html_listing,
    parse_deepseek_links,
    parse_html_listing,
)

BEIJING = timezone(timedelta(hours=8))
REPAIRED_IDS = {
    "anthropic",
    "anthropic_news",
    "jiqizhixin",
    "mistral",
    "xai",
    "deepseek",
    "ms_ai",
    "hf_papers",
    "parliament",
}


def mock_http(handler):
    real_client = httpx.AsyncClient

    def client(**kwargs):
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    return patch("httpx.AsyncClient", client)


def mistral(fetch, disabled=None) -> SourceSpec:
    return SourceSpec(
        id="mistral",
        name="Mistral",
        fetch=fetch,
        params={"name": "Mistral", "url": "https://mistral.ai/news/rss"},
        disabled=disabled,
    )


class SourceStatusTest(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_source_is_skipped(self):
        result = await run_source(mistral(lambda config: None, disabled="HTTP 404"))
        self.assertEqual((result.status, result.items, result.error), ("skipped", [], None))

    async def test_empty_success_is_no_candidates(self):
        async def fetch(_config):
            return []

        result = await run_source(mistral(fetch))
        self.assertEqual(result.status, "no_candidates")

    async def test_exception_is_failed(self):
        async def fetch(_config):
            raise SourceFetchError("HTTP 503")

        result = await run_source(mistral(fetch))
        self.assertEqual((result.status, result.error), ("failed", "HTTP 503"))

    async def test_exception_without_message_reports_its_type(self):
        async def fetch(_config):
            raise httpx.ReadTimeout("")

        result = await run_source(mistral(fetch))
        self.assertEqual((result.status, result.error), ("failed", "ReadTimeout"))

    async def test_items_are_success(self):
        received = []

        async def fetch(config):
            received.append(config)
            return [NewsItem(title="A", url="https://example.com/a", source="Mistral")]

        result = await run_source(mistral(fetch))
        self.assertEqual(result.status, "success")
        self.assertEqual(result.to_record()["item_count"], 1)
        self.assertEqual(received, [{"name": "Mistral", "url": "https://mistral.ai/news/rss"}])


class SourceSpecTest(unittest.TestCase):
    def test_entries_parse_in_order_with_fetcher_params(self):
        specs = parse_source_specs({
            "mistral": {"kind": "feed", "name": "Mistral", "url": "https://mistral.ai/news/rss"},
            "xai": {
                "kind": "feed",
                "name": "xAI",
                "url": "https://x.ai/news/rss.xml",
                "disabled": "HTTP 404",
            },
        })
        self.assertEqual(
            specs,
            [
                SourceSpec(
                    id="mistral",
                    name="Mistral",
                    fetch=fetch_feed,
                    params={"name": "Mistral", "url": "https://mistral.ai/news/rss"},
                    disabled=None,
                ),
                SourceSpec(
                    id="xai",
                    name="xAI",
                    fetch=fetch_feed,
                    params={"name": "xAI", "url": "https://x.ai/news/rss.xml"},
                    disabled="HTTP 404",
                ),
            ],
        )

    def test_malformed_entries_fail_before_any_fetch(self):
        cases = {
            "enabled key": ({"kind": "feed", "name": "X", "enabled": False}, r"sources\.x.*enabled"),
            "empty reason": ({"kind": "feed", "name": "X", "disabled": ""}, r"sources\.x\.disabled"),
            "unknown kind": ({"kind": "rss", "name": "X"}, r"sources\.x\.kind"),
            "missing name": ({"kind": "feed"}, r"sources\.x\.name"),
            "not a mapping": ("https://x.example/feed", r"sources\.x must be a mapping"),
        }
        for label, (entry, message) in cases.items():
            with self.subTest(label), self.assertRaisesRegex(SourceConfigError, message):
                parse_source_specs({"x": entry})


class SourceRegistryTest(unittest.IsolatedAsyncioTestCase):
    async def test_config_runs_repaired_ids_and_never_requests_disabled_hosts(self):
        config = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
        specs = parse_source_specs(config["sources"])
        self.assertLessEqual(REPAIRED_IDS, {spec.id for spec in specs})
        self.assertEqual(
            {spec.id for spec in specs if spec.disabled},
            {"jiqizhixin", "xai", "parliament"},
        )

        requested_hosts = []

        def handler(request):
            requested_hosts.append(request.url.host)
            return httpx.Response(404)

        with mock_http(handler):
            results = await asyncio.gather(*(run_source(spec) for spec in specs))

        outcome = {result.id: (result.status, result.error) for result in results}
        self.assertEqual(outcome["parliament"], ("skipped", None))
        self.assertEqual(outcome["xai"], ("skipped", None))
        self.assertEqual(outcome["jiqizhixin"], ("skipped", None))
        self.assertEqual(outcome["mistral"], ("failed", "mistral.ai: HTTP 404"))
        self.assertEqual(outcome["ms_ai"], ("failed", "news.microsoft.com: HTTP 404"))
        self.assertEqual(outcome["anthropic"], ("failed", "www.anthropic.com: HTTP 404"))
        self.assertEqual(outcome["deepseek"], ("failed", "api-docs.deepseek.com: HTTP 404"))
        self.assertNotIn("committees.parliament.uk", requested_hosts)
        self.assertNotIn("x.ai", requested_hosts)
        self.assertNotIn("www.jiqizhixin.com", requested_hosts)

    async def test_each_configured_url_is_requested_once(self):
        config = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
        specs = parse_source_specs(config["sources"])
        requested = []

        def handler(request):
            requested.append(str(request.url))
            return httpx.Response(404)

        with mock_http(handler):
            await asyncio.gather(*(run_source(spec) for spec in specs))

        counts = {}
        for url in requested:
            counts[url] = counts.get(url, 0) + 1
        self.assertEqual(
            {url: count for url, count in counts.items() if count > 1},
            {},
        )
        self.assertEqual(counts.get("https://huggingface.co/api/daily_papers"), 1)


MICROSOFT_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Microsoft Source</title>
<item>
  <title>Introducing Microsoft’s latest custom chip: Maia 300</title>
  <link>https://news.microsoft.com/maia-2026/</link>
  <pubDate>Tue, 13 Oct 2026 16:00:00 +0000</pubDate>
  <description>&lt;p&gt;Maia 300 is here.&lt;/p&gt;</description>
</item>
<item><title>Undated</title><link>https://news.microsoft.com/undated/</link></item>
</channel></rss>
"""

ANTHROPIC_NEWS = """<html><body>
<nav><a href="/news">News</a><a href="/news/press-kit/logos"><time>Oct 1, 2026</time>Logos</a></nav>
<a href="/news/cyber-verification-program"><h2>Expanding the Cyber Verification Program</h2>
  <div><span>Announcements</span><time>Oct 6, 2026</time></div><p>We are launching a program.</p></a>
<a href="/news/2026-usage-policy-update"><div><time>Oct 8, 2026</time><span>Announcements</span></div>
  <span>2026 Usage Policy update</span></a>
<a href="https://www.anthropic.com/news/2026-usage-policy-update"><time>Oct 8, 2026</time><span>Again</span></a>
<a href="https://example.com/news/elsewhere"><time>Oct 9, 2026</time><span>Other host</span></a>
<a href="/news/undated"><span>No date here</span></a>
</body></html>
"""

ANTHROPIC_ENGINEERING = """<html><body>
<a href="/engineering/how-we-contain-claude"><div><img alt="Illustration" src="a.svg"/></div>
  <div><h3>How we contain Claude across products</h3>
  <div><time dateTime="2026-05-25">May 25, 2026</time></div></div></a>
</body></html>
"""

DEEPSEEK_UPDATES = """<html><body>
<nav><a href="/news/news260910">News</a><a href="/quick_start/pricing">Pricing</a></nav>
<li>2025-12-01 See <a href="/news/news251201">this documentation</a></li>
<li>2026-09-10 See <a href="/news/news260910">this documentation</a></li>
<li>2026-08-13 See <a href="/news/news260813">this documentation</a></li>
<li>2026-04-24 See <a href="https://api-docs.deepseek.com/news/news260424/">this documentation</a></li>
</body></html>
"""


class FetcherTest(unittest.IsolatedAsyncioTestCase):
    async def test_feed_sends_project_user_agent(self):
        def handler(request):
            if request.headers["user-agent"] != "ai-daily-brief/1.0":
                return httpx.Response(403)
            return httpx.Response(200, text=MICROSOFT_FEED)

        with mock_http(handler):
            items = await fetch_feed({
                "name": "Microsoft AI",
                "url": "https://news.microsoft.com/source/topics/ai/feed/",
            })

        self.assertEqual(
            [(item.title, item.url, item.source, item.summary, item.published) for item in items],
            [(
                "Introducing Microsoft’s latest custom chip: Maia 300",
                "https://news.microsoft.com/maia-2026/",
                "Microsoft AI",
                "Maia 300 is here.",
                datetime(2026, 10, 13, 16, 0, tzinfo=timezone.utc),
            )],
        )

    async def test_feed_errors_name_host_and_status(self):
        cases = {
            "https://mistral.ai/news/rss": (
                httpx.Response(404),
                "mistral.ai: HTTP 404",
            ),
            "https://www.jiqizhixin.com/rss": (
                httpx.Response(200, text="<!doctype html><html><body><h1>数据服务</h1></body></html>"),
                "www.jiqizhixin.com: HTTP 200 but not a feed",
            ),
        }
        for url, (response, message) in cases.items():
            with self.subTest(url), mock_http(lambda request, r=response: r):
                with self.assertRaises(SourceFetchError) as caught:
                    await fetch_feed({"name": "X", "url": url})
                self.assertEqual(str(caught.exception), message)

    async def test_html_listing_without_dated_links_is_failure(self):
        with mock_http(lambda request: httpx.Response(200, text="<html><a href='/news/x'>X</a></html>")):
            with self.assertRaises(SourceFetchError) as caught:
                await fetch_html_listing({"name": "Anthropic", "url": "https://www.anthropic.com/news"})
        self.assertEqual(
            str(caught.exception), "www.anthropic.com: HTTP 200 but no dated article links"
        )

    def test_listing_parser_reads_dated_article_links(self):
        news = parse_html_listing(ANTHROPIC_NEWS, "https://www.anthropic.com/news", "Anthropic")
        engineering = parse_html_listing(
            ANTHROPIC_ENGINEERING, "https://www.anthropic.com/engineering", "Anthropic Engineering"
        )
        self.assertEqual(
            [(item.title, item.url, item.source, item.published) for item in news + engineering],
            [
                (
                    "Expanding the Cyber Verification Program",
                    "https://www.anthropic.com/news/cyber-verification-program",
                    "Anthropic",
                    datetime(2026, 10, 6, 20, 0, tzinfo=BEIJING),
                ),
                (
                    "2026 Usage Policy update",
                    "https://www.anthropic.com/news/2026-usage-policy-update",
                    "Anthropic",
                    datetime(2026, 10, 8, 20, 0, tzinfo=BEIJING),
                ),
                (
                    "How we contain Claude across products",
                    "https://www.anthropic.com/engineering/how-we-contain-claude",
                    "Anthropic Engineering",
                    datetime(2026, 5, 25, 20, 0, tzinfo=BEIJING),
                ),
            ],
        )

    def test_deepseek_links_are_unique_newest_first_with_slug_dates(self):
        self.assertEqual(
            parse_deepseek_links(DEEPSEEK_UPDATES, "https://api-docs.deepseek.com/updates/"),
            [
                ("https://api-docs.deepseek.com/news/news260910", datetime(2026, 9, 10, 20, 0, tzinfo=BEIJING)),
                ("https://api-docs.deepseek.com/news/news260813", datetime(2026, 8, 13, 20, 0, tzinfo=BEIJING)),
                ("https://api-docs.deepseek.com/news/news260424/", datetime(2026, 4, 24, 20, 0, tzinfo=BEIJING)),
                ("https://api-docs.deepseek.com/news/news251201", datetime(2025, 12, 1, 20, 0, tzinfo=BEIJING)),
            ],
        )

    async def test_deepseek_titles_come_from_three_newest_pages(self):
        pages = {
            "/updates/": DEEPSEEK_UPDATES,
            "/news/news260910": "<h1>DeepSeek-V4.1-Flash: Smarter, Faster, More Efficient</h1>",
            "/news/news260424/": "<article><h1>DeepSeek-V4 <em>Preview</em></h1><h1>Second</h1></article>",
            "/news/news251201": "<h1>Should not be fetched</h1>",
        }
        requested = []

        def handler(request):
            requested.append(request.url.path)
            if request.url.path in pages:
                return httpx.Response(200, text=pages[request.url.path])
            return httpx.Response(404)

        with mock_http(handler), self.assertLogs("sources", level="WARNING"):
            items = await fetch_deepseek_updates({
                "name": "DeepSeek",
                "url": "https://api-docs.deepseek.com/updates/",
                "max_items": 3,
            })

        self.assertEqual(
            requested,
            ["/updates/", "/news/news260910", "/news/news260813", "/news/news260424/"],
        )
        self.assertEqual(
            [(item.title, item.url, item.source, item.published) for item in items],
            [
                (
                    "DeepSeek-V4.1-Flash: Smarter, Faster, More Efficient",
                    "https://api-docs.deepseek.com/news/news260910",
                    "DeepSeek",
                    datetime(2026, 9, 10, 20, 0, tzinfo=BEIJING),
                ),
                (
                    "DeepSeek-V4 Preview",
                    "https://api-docs.deepseek.com/news/news260424/",
                    "DeepSeek",
                    datetime(2026, 4, 24, 20, 0, tzinfo=BEIJING),
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()
