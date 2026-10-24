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
    fetch_html_pages,
    fetch_huggingface,
    fetch_reddit,
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
            {"jiqizhixin", "parliament"},
        )

        requested_hosts = []

        def handler(request):
            requested_hosts.append(request.url.host)
            return httpx.Response(404)

        with mock_http(handler):
            results = await asyncio.gather(*(run_source(spec) for spec in specs))

        outcome = {result.id: (result.status, result.error) for result in results}
        self.assertEqual(outcome["parliament"], ("skipped", None))
        self.assertEqual(outcome["xai"], ("failed", "xAI: no readable article page"))
        self.assertEqual(outcome["jiqizhixin"], ("skipped", None))
        self.assertEqual(outcome["mistral"], ("failed", "mistral.ai: HTTP 404"))
        self.assertEqual(outcome["ms_ai"], ("failed", "news.microsoft.com: HTTP 404"))
        self.assertEqual(outcome["anthropic"], ("failed", "www.anthropic.com: HTTP 404"))
        self.assertEqual(outcome["deepseek"], ("failed", "api-docs.deepseek.com: HTTP 404"))
        self.assertNotIn("committees.parliament.uk", requested_hosts)
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
        self.assertEqual(counts.get("https://x.ai/news/grok-voice-3"), 1)
        self.assertEqual(counts.get("https://x.ai/news/x-subscription"), 1)
        self.assertEqual(counts.get("https://x.ai/news/grok-4-7"), 1)
        self.assertNotIn("https://x.ai/news", requested)
        self.assertNotIn("https://x.ai/news/", requested)
        self.assertNotIn("https://x.ai/blog", requested)
        self.assertNotIn("https://x.ai/news/rss.xml", requested)

    async def test_edition_gather_follows_config_yaml(self):
        steps = Path("delivery/STEPS.md").read_text(encoding="utf-8")
        bridge = Path("integrations/grok_bot/README.md").read_text(encoding="utf-8")
        self.assertIn("python main.py --sources-only --dry-run", steps)
        self.assertIn("config.yaml", steps)
        self.assertIn("delivery/STEPS.md", bridge)
        self.assertNotIn("使用自身模型采集公开来源", bridge)
        self.assertNotIn("只使用自身采集的公开来源", bridge)

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
        self.assertEqual({url: count for url, count in counts.items() if count != 1}, {})
        for url in (
            "https://www.anthropic.com/engineering",
            "https://www.anthropic.com/news",
            "https://news.microsoft.com/source/topics/ai/feed/",
            "https://x.ai/news/grok-voice-3",
            "https://x.ai/news/x-subscription",
            "https://x.ai/news/grok-4-7",
        ):
            self.assertEqual(counts.get(url), 1, url)
        for url in (
            "https://www.anthropic.com/rss.xml",
            "https://www.anthropic.com/news/rss.xml",
            "https://ai.meta.com/blog/rss/",
            "https://blogs.microsoft.com/ai/feed/",
            "https://www.reuters.com/technology/artificial-intelligence/rss",
            "https://x.ai/news",
            "https://x.ai/news/rss.xml",
            "https://x.com/xai",
            "https://x.com/SpaceXSI",
        ):
            self.assertNotIn(url, requested, url)


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

REDDIT_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>LocalLlama</title>
  <entry>
    <title>[Bi-Weekly Megathread] Project Showcase</title>
    <link href="https://www.reddit.com/r/LocalLLaMA/comments/1x9yg86/biweekly_megathread_project_showcase/"/>
    <published>2026-10-19T14:30:51+00:00</published>
    <content type="html">&lt;p&gt;Share projects.&lt;/p&gt;</content>
  </entry>
  <entry>
    <title>Vibecoding your own inference engine</title>
    <link href="https://www.reddit.com/r/LocalLLaMA/comments/1xdh57e/vibecoding/"/>
    <published>2026-10-23T15:26:47+00:00</published>
    <content type="html">&lt;p&gt;It was not easy.&lt;/p&gt;</content>
  </entry>
  <entry>
    <title>Undated post</title>
    <link href="https://www.reddit.com/r/LocalLLaMA/comments/undated/"/>
  </entry>
</feed>
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

    async def test_html_pages_read_the_article_and_skip_blocked_indexes(self):
        html = """<html><script type="application/ld+json">
        {"datePublished":"2026-10-14T00:00:00Z"}</script>
        <h1>Introducing Grok Voice 3.0</h1></html>"""
        requested = []

        def handler(request):
            requested.append(request.url.path)
            return httpx.Response(200, text=html)

        with mock_http(handler):
            items = await fetch_html_pages({
                "name": "xAI",
                "pages": [
                    "https://x.ai/news",
                    "https://x.ai/blog",
                    "https://x.ai/news/grok-voice-3",
                ],
            })

        self.assertEqual(requested, ["/news/grok-voice-3"])
        self.assertEqual(
            [(item.title, item.url, item.source, item.published) for item in items],
            [(
                "Introducing Grok Voice 3.0",
                "https://x.ai/news/grok-voice-3",
                "xAI",
                datetime(2026, 10, 14, 0, 0, tzinfo=timezone.utc),
            )],
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

    def test_current_anthropic_pages_keep_dated_articles(self):
        fixtures = Path(__file__).resolve().parent / "fixtures"
        news = parse_html_listing(
            (fixtures / "anthropic-news-2026-10-24.html").read_text(encoding="utf-8"),
            "https://www.anthropic.com/news",
            "Anthropic",
        )
        engineering = parse_html_listing(
            (fixtures / "anthropic-engineering-2026-10-24.html").read_text(encoding="utf-8"),
            "https://www.anthropic.com/engineering",
            "Anthropic Engineering",
        )
        news_rows = {(item.title, item.url, item.published) for item in news}
        engineering_rows = {(item.title, item.url, item.published) for item in engineering}
        self.assertGreaterEqual(len(news), 20)
        self.assertGreaterEqual(len(engineering), 20)
        self.assertTrue(all(item.published.tzinfo is not None for item in news + engineering))
        for row in (
            (
                "Investing in the public benefit",
                "https://www.anthropic.com/news/public-benefit-fund",
                datetime(2026, 10, 18, 20, 0, tzinfo=BEIJING),
            ),
            (
                "2026 Usage Policy update",
                "https://www.anthropic.com/news/2026-usage-policy-update",
                datetime(2026, 10, 8, 20, 0, tzinfo=BEIJING),
            ),
            (
                "The Situation Report",
                "https://www.anthropic.com/features/ebola-response",
                datetime(2026, 9, 21, 20, 0, tzinfo=BEIJING),
            ),
            (
                "Scenarios for our economic future",
                "https://www.anthropic.com/institute/econ-scenarios",
                datetime(2026, 9, 9, 20, 0, tzinfo=BEIJING),
            ),
        ):
            self.assertIn(row, news_rows)
        for row in (
            (
                "How we contain Claude across products",
                "https://www.anthropic.com/research/how-we-contain-claude",
                datetime(2026, 5, 25, 20, 0, tzinfo=BEIJING),
            ),
            (
                "Redesigning source control at Anthropic",
                "https://www.anthropic.com/research/redesigning-source-control",
                datetime(2026, 10, 13, 20, 0, tzinfo=BEIJING),
            ),
        ):
            self.assertIn(row, engineering_rows)
        urls = {item.url for item in news + engineering}
        self.assertNotIn("https://www.anthropic.com/news/announcements", urls)
        self.assertNotIn("https://www.anthropic.com/news/product", urls)
        self.assertNotIn("https://www.anthropic.com/research/team", urls)

    async def test_huggingface_trending_models_use_trending_score(self):
        requested = []

        def handler(request):
            requested.append(dict(request.url.params))
            if (
                request.url.params.get("sort") == "trendingScore"
                and request.url.params.get("full") == "true"
            ):
                return httpx.Response(200, json=[
                    {
                        "modelId": "deepseek-ai/DeepSeek-V4.1-Flash-1017",
                        "likes": 2582,
                        "lastModified": "2026-10-17T05:46:55.000Z",
                        "tags": ["text-generation", "safetensors", "region:us"],
                    },
                    {
                        "modelId": "example/missing-date",
                        "likes": 10,
                        "tags": ["text-generation"],
                    },
                ])
            return httpx.Response(
                400,
                json={"error": "Invalid sort parameter: trending"},
                headers={"x-error-message": "Invalid sort parameter: trending"},
            )

        with mock_http(handler):
            items = await fetch_huggingface({
                "daily_papers": False,
                "trending_models": True,
                "top_n": 10,
            })

        self.assertEqual(requested, [{"sort": "trendingScore", "limit": "10", "full": "true"}])
        self.assertEqual(
            [(item.title, item.url, item.source, item.score, item.published, item.tags) for item in items],
            [(
                "🤗 deepseek-ai/DeepSeek-V4.1-Flash-1017",
                "https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash-1017",
                "HuggingFace Models",
                2582,
                datetime(2026, 10, 17, 5, 46, 55, tzinfo=timezone.utc),
                ["huggingface", "model", "text-generation", "safetensors", "region:us"],
            )],
        )

    async def test_reddit_reads_public_atom_feed(self):
        requested = []

        def handler(request):
            requested.append(str(request.url))
            if request.url.path == "/r/LocalLLaMA/.rss":
                return httpx.Response(200, text=REDDIT_ATOM)
            return httpx.Response(403)

        with mock_http(handler):
            items = await fetch_reddit({
                "subreddits": ["LocalLLaMA"],
                "top_n": 15,
                "min_score": 10000,
            })

        self.assertEqual(requested, ["https://www.reddit.com/r/LocalLLaMA/.rss"])
        self.assertEqual(
            [(item.title, item.url, item.source, item.summary, item.published) for item in items],
            [
                (
                    "[Bi-Weekly Megathread] Project Showcase",
                    "https://www.reddit.com/r/LocalLLaMA/comments/1x9yg86/biweekly_megathread_project_showcase/",
                    "Reddit r/LocalLLaMA",
                    "Share projects.",
                    datetime(2026, 10, 19, 14, 30, 51, tzinfo=timezone.utc),
                ),
                (
                    "Vibecoding your own inference engine",
                    "https://www.reddit.com/r/LocalLLaMA/comments/1xdh57e/vibecoding/",
                    "Reddit r/LocalLLaMA",
                    "It was not easy.",
                    datetime(2026, 10, 23, 15, 26, 47, tzinfo=timezone.utc),
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()
