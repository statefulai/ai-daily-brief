import hashlib
import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from edition import EditionError, build_edition, write_edition
from outputs import write_web_edition
from scripts.edition_ci import (
    _history_index,
    build_site,
    render_edition_file,
    validate_changed_paths,
    validate_tree,
)

APPROVED_MASTHEAD_SHA256 = (
    "ae5eaffbcfefbb62d1aa3998c2c160baa7566e10b924cb363aa4f6b00cb434aa"
)
WEEKDAYS_ZH = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
BEIJING = timezone(timedelta(hours=8))


def event() -> dict:
    return {
        "id": "event-1",
        "placement": "lead",
        "window": "in_window",
        "title": "一条已核验新闻",
        "kicker": "官方更新",
        "facts": ["官方发布了一项更新。"],
        "conditions": [],
        "background": [],
        "sources": [{
            "label": "官方来源",
            "url": "https://example.com/news",
            "kind": "primary",
            "published_at": "2026-09-16T01:00:00+08:00",
            "verified": True,
            "note": None,
        }],
    }


def document() -> dict:
    return build_edition(
        edition_id="2026-09-16",
        cutoff_at="2026-09-16T14:00:00+08:00",
        generated_at="2026-09-16T14:05:00+08:00",
        sources=[{"id": "official", "status": "success", "item_count": 1, "error": None}],
        events=[event()],
    )


def make_event(
    event_id: str,
    title: str,
    *,
    placement: str = "story",
    window: str = "in_window",
    kicker: str = "分类",
    fact: str = "一条事实。",
    conditions: list[str] | None = None,
) -> dict:
    return {
        "id": event_id,
        "placement": placement,
        "window": window,
        "title": title,
        "kicker": kicker,
        "facts": [fact, "第二段不应进入首页摘录。"],
        "conditions": [] if conditions is None else conditions,
        "background": [],
        "sources": [{
            "label": "官方来源",
            "url": f"https://example.com/{event_id}",
            "kind": "primary",
            "published_at": "2026-09-16T01:00:00+08:00",
            "verified": True,
            "note": None,
        }],
    }


def make_edition(
    edition_id: str,
    events: list[dict],
    *,
    cutoff_at: str,
    sources: list[dict] | None = None,
) -> dict:
    return build_edition(
        edition_id=edition_id,
        cutoff_at=cutoff_at,
        generated_at=cutoff_at,
        sources=sources or [{
            "id": "official",
            "status": "success",
            "item_count": 1,
            "error": None,
        }],
        events=events,
    )


def publish(root: Path, edition: dict) -> None:
    write_edition(root, edition)
    write_web_edition(root, edition)


def file_digests(root: Path) -> dict[str, str]:
    digests = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digests[path.relative_to(root).as_posix()] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    return digests


def coverage_note(edition: dict) -> str:
    cutoff = datetime.fromisoformat(
        edition["cutoff_at"].replace("Z", "+00:00")
    ).astimezone(BEIJING)
    in_window = sum(item["window"] == "in_window" for item in edition["events"])
    recent = sum(item["window"] == "recent" for item in edition["events"])
    note = (
        f"截至 {cutoff.month} 月 {cutoff.day} 日 {cutoff:%H:%M}（北京时间）。"
        f"本期收录 {in_window} 条窗口内动态"
    )
    if recent:
        note += f"，另附 {recent} 条近期选读"
    failed = sum(item["status"] == "failed" for item in edition["sources"])
    if failed:
        note += f"；另有 {failed} 个来源采集失败，本期覆盖可能不完整"
    return note + "。"


def weekday_label(edition_id: str) -> str:
    return WEEKDAYS_ZH[datetime.strptime(edition_id, "%Y-%m-%d").weekday()]


def region(html: str, start: str, end: str | None) -> str:
    start_at = html.index(f'id="{start}"')
    end_at = html.index(f'id="{end}"') if end else len(html)
    return html[start_at:end_at]


class EditionCITest(unittest.TestCase):
    def test_render_normalizes_agent_edition_and_writes_html(self):
        payload = document()
        payload.pop("content_hash")
        with tempfile.TemporaryDirectory() as directory:
            editions = Path(directory) / "editions"
            target = editions / payload["edition_id"]
            target.mkdir(parents=True)
            edition_path = target / "edition.json"
            edition_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            rendered = render_edition_file(edition_path)
            validated = validate_tree(editions)

            self.assertEqual(rendered["edition_id"], "2026-09-16")
            self.assertEqual(validated[0]["content_hash"], rendered["content_hash"])
            self.assertEqual(
                {path.name for path in target.iterdir()},
                {"edition.json", "index.html"},
            )

    def test_validate_tree_accepts_deterministic_two_file_edition(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_edition(root, document())
            write_web_edition(root, document())

            result = validate_tree(root)

        self.assertEqual([item["edition_id"] for item in result], ["2026-09-16"])

    def test_validate_tree_rejects_unverified_primary_source(self):
        payload = document()
        payload["events"][0]["sources"][0]["verified"] = False
        payload.pop("content_hash")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_edition(root, payload)
            write_web_edition(root, payload)

            with self.assertRaisesRegex(EditionError, "no verified primary source"):
                validate_tree(root)

    def test_validate_tree_rejects_stale_html(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_edition(root, document())
            target = write_web_edition(root, document())
            target.write_text("stale", encoding="utf-8")

            with self.assertRaisesRegex(EditionError, "deterministic render"):
                validate_tree(root)

    def test_validate_tree_requires_persisted_content_hash(self):
        payload = document()
        payload.pop("content_hash")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / payload["edition_id"]
            target.mkdir(parents=True)
            (target / "edition.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            write_web_edition(root, payload)

            with self.assertRaisesRegex(EditionError, "must include content_hash"):
                validate_tree(root)

    def test_scope_allows_one_complete_edition_only(self):
        validate_changed_paths([
            "editions/2026-09-16/edition.json",
            "editions/2026-09-16/index.html",
        ])
        with self.assertRaisesRegex(EditionError, "outside"):
            validate_changed_paths([
                "editions/2026-09-16/edition.json",
                "editions/2026-09-16/index.html",
                "main.py",
            ])
        with self.assertRaisesRegex(EditionError, "one edition date"):
            validate_changed_paths([
                "editions/2026-09-16/edition.json",
                "editions/2026-09-16/index.html",
                "editions/2026-09-17/edition.json",
                "editions/2026-09-17/index.html",
            ])
        with self.assertRaisesRegex(EditionError, "may not be deleted"):
            validate_changed_paths(
                [], deleted_paths=["editions/2026-09-16/index.html"]
            )

    def test_build_site_copies_only_public_files_and_writes_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            editions = root / "editions"
            site = root / "site"
            write_edition(editions, document())
            write_web_edition(editions, document())

            built = build_site(editions, site)

            self.assertEqual(len(built), 1)
            self.assertEqual(
                {path.name for path in (site / "editions" / "2026-09-16").iterdir()},
                {"edition.json", "index.html"},
            )
            index = (site / "index.html").read_text(encoding="utf-8")
            self.assertIn("editions/2026-09-16/", index)
            self.assertNotIn("daily-brief.md", index)
            json.loads((site / "editions" / "2026-09-16" / "edition.json").read_text())

    def test_build_site_publishes_normalized_edition_json(self):
        payload = document()
        payload["internal_note"] = "not public"
        payload["events"][0]["internal_draft"] = "not public"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            editions = root / "editions"
            target = editions / payload["edition_id"]
            site = root / "site"
            target.mkdir(parents=True)
            (target / "edition.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            write_web_edition(editions, payload)

            build_site(editions, site)

            published = json.loads(
                (site / "editions" / "2026-09-16" / "edition.json").read_text()
            )
            self.assertNotIn("internal_note", published)
            self.assertNotIn("internal_draft", published["events"][0])
            self.assertTrue(published["content_hash"].startswith("sha256:"))

    def test_workflow_protects_push_history_without_auto_enabling_pages(self):
        workflow = Path(".github/workflows/edition-ci-pages.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("Protect edition history on main push", workflow)
        self.assertIn('${{ github.event.before }}', workflow)
        self.assertEqual(workflow.count('"contributions.py"'), 2)
        self.assertNotIn("enablement: true", workflow)

    def test_home_template_drops_prototype_snapshot(self):
        template = Path("templates/home.html").read_text(encoding="utf-8")
        self.assertNotIn("noindex", template)
        self.assertNotIn("prototype", template.lower())
        self.assertNotIn("待确认", template)
        self.assertNotIn("2026-09-25", template)
        self.assertNotIn("brief.sanze.dev", template)
        self.assertNotIn("只收录重要变化", template)
        self.assertIn("assets/masthead-art.webp", template)
        self.assertIn("编选原则", template)
        self.assertIn("#faf7f0", template)
        self.assertIn("#e7e3db", template)
        self.assertIn("#252621", template)
        self.assertIn("#91472f", template)
        self.assertIn("1120px", template)
        self.assertIn("max-width: 820px", template)
        self.assertIn("max-width: 680px", template)
        self.assertIn("width: 92px", template)
        self.assertIn("width: 36px", template)
        for anchor in ("#latest", "#archive", "#principles"):
            self.assertIn(f'href="{anchor}"', template)

    def test_masthead_is_approved_bytes_and_build_copies_only_that_asset(self):
        asset = Path("templates/assets/masthead-art.webp")
        digest = hashlib.sha256(asset.read_bytes()).hexdigest()
        self.assertEqual(digest, APPROVED_MASTHEAD_SHA256)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            editions = root / "editions"
            site = root / "site"
            publish(editions, document())
            build_site(editions, site)
            copied = site / "assets" / "masthead-art.webp"
            self.assertEqual(hashlib.sha256(copied.read_bytes()).hexdigest(), digest)
            files = sorted(
                path.relative_to(site).as_posix()
                for path in site.rglob("*")
                if path.is_file()
            )
            self.assertEqual(
                files,
                [
                    "assets/masthead-art.webp",
                    "editions/2026-09-16/edition.json",
                    "editions/2026-09-16/index.html",
                    "index.html",
                ],
            )
            homepage = (site / "index.html").read_text(encoding="utf-8")
            self.assertIn('src="assets/masthead-art.webp"', homepage)
            self.assertNotIn('src="/', homepage)
            self.assertNotIn("https://brief.sanze.dev", homepage)
            self.assertNotIn("noindex", homepage)
            edition_names = {
                path.name for path in (site / "editions" / "2026-09-16").iterdir()
            }
            self.assertEqual(edition_names, {"edition.json", "index.html"})

    def test_missing_masthead_fails_without_replacing_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            editions = root / "editions"
            site = root / "site"
            publish(editions, document())
            build_site(editions, site)
            before = (site / "index.html").read_bytes()
            with patch("scripts.edition_ci._masthead_path", return_value=root / "missing.webp"):
                with self.assertRaisesRegex(EditionError, "masthead is missing"):
                    build_site(editions, site)
            self.assertEqual((site / "index.html").read_bytes(), before)
            self.assertTrue((site / "assets" / "masthead-art.webp").is_file())

    def test_build_refuses_to_publish_into_edition_history(self):
        with tempfile.TemporaryDirectory() as directory:
            editions = Path(directory) / "editions"
            publish(editions, document())
            before = file_digests(editions)
            with self.assertRaisesRegex(EditionError, "refusing"):
                build_site(editions, editions)
            with self.assertRaisesRegex(EditionError, "refusing"):
                build_site(editions, editions / "nested")
            self.assertEqual(file_digests(editions), before)

    def test_empty_and_single_edition_home_states(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            editions = root / "editions"
            site = root / "site"
            editions.mkdir()
            build_site(editions, site)
            empty = (site / "index.html").read_text(encoding="utf-8")
            empty_body = empty.split("<body", 1)[1]
            self.assertIn("尚无公开期次", empty_body)
            self.assertNotIn('class="read-edition"', empty_body)
            self.assertNotIn("editions/", empty_body)
            self.assertNotIn("archive-month", empty_body)
            self.assertIsNone(re.search(r"\d{4}-\d{2}-\d{2}", empty_body))
            for anchor in ("latest", "archive", "principles", "main"):
                self.assertIn(f'id="{anchor}"', empty)
            self.assertIn("此前已刊 0 期", empty)
            self.assertIn("AI 日报 · 最新一期与往期", empty)

            publish(editions, document())
            build_site(editions, site)
            single = (site / "index.html").read_text(encoding="utf-8")
            latest = region(single, "latest", "archive")
            archive = region(single, "archive", "principles")
            self.assertIn("暂无更早期次", archive)
            self.assertNotIn("archive-month", archive)
            self.assertNotIn("editions/", archive)
            self.assertIn('href="editions/2026-09-16/"', latest)
            self.assertIn("一条已核验新闻", latest)
            self.assertIn("2026.09.16", latest)
            self.assertIn(weekday_label("2026-09-16"), latest)
            self.assertIn("此前已刊 0 期", archive)

    def test_home_lead_sidebar_escape_and_archive_grouping(self):
        secret = "SECRET_SOURCE_ERROR_do_not_show"
        long_zh = "长中文标题" + ("测" * 80)
        long_en = "VeryLongEnglishTitle " * 12
        malicious_title = long_zh + '<script>alert(1)</script>'
        malicious_kicker = '"><img src=x onerror=alert(1)>'
        malicious_fact = "Tom & Jerry 称可能尚未 <b>hidden</b>"
        lead_edition = make_edition(
            "2026-08-02",
            [
                make_event("first", "第一条不是头条", placement="story", fact="第一条事实"),
                make_event(
                    "lead",
                    malicious_title,
                    placement="lead",
                    kicker=malicious_kicker,
                    fact=malicious_fact,
                    conditions=[],
                ),
                make_event("third", long_en, placement="story"),
                make_event("fourth", "第四条", placement="story"),
                make_event("fifth", "第五条不应出现在侧栏", placement="story"),
                make_event(
                    "sixth",
                    "第六条近期不应出现在侧栏",
                    placement="story",
                    window="recent",
                ),
            ],
            cutoff_at="2026-08-02T09:05:00+08:00",
            sources=[
                {"id": "official", "status": "success", "item_count": 1, "error": None},
                {"id": "broken", "status": "failed", "item_count": 0, "error": secret},
            ],
        )
        plain = make_edition(
            "2026-08-01",
            [make_event("only", "只有头条", placement="lead", conditions=["不应漏掉的条件"])],
            cutoff_at="2026-08-01T08:00:00+08:00",
        )
        few = make_edition(
            "2025-12-31",
            [
                make_event("a", "无显式头条的第一条", placement="story"),
                make_event("b", "无显式头条的第二条", placement="story"),
                make_event("c", "无显式头条的第三条", placement="story"),
            ],
            cutoff_at="2025-12-31T08:00:00+08:00",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            editions = root / "editions"
            site = root / "site"
            publish(editions, few)
            publish(editions, lead_edition)
            publish(editions, plain)
            before = file_digests(editions)
            build_site(editions, site)
            self.assertEqual(file_digests(editions), before)
            html = (site / "index.html").read_text(encoding="utf-8")
            latest = region(html, "latest", "archive")
            archive = region(html, "archive", "principles")
            self.assertIn(coverage_note(lead_edition), latest)
            self.assertIn("5 条窗口内动态", latest)
            self.assertIn("另附 1 条近期选读", latest)
            self.assertIn("另有 1 个来源采集失败", latest)
            self.assertNotIn(secret, html)
            self.assertNotIn("<script>", html)
            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", latest)
            self.assertIn("Tom &amp; Jerry 称可能尚未", latest)
            self.assertNotIn("<b>hidden</b>", latest)
            self.assertIn("&quot;&gt;&lt;img src=x onerror=alert(1)&gt;", latest)
            self.assertNotIn("<img", latest)
            self.assertNotIn('class="scope"', latest)
            self.assertIn("6 条内容", latest)
            self.assertIn('href="editions/2026-08-02/#event-1"', latest)
            self.assertIn('href="editions/2026-08-02/#event-3"', latest)
            self.assertIn('href="editions/2026-08-02/#event-4"', latest)
            self.assertNotIn('href="editions/2026-08-02/#event-2"', latest)
            self.assertNotIn('href="editions/2026-08-02/#event-5"', latest)
            self.assertNotIn("第五条不应出现在侧栏", latest)
            self.assertNotIn("第六条近期不应出现在侧栏", latest)
            self.assertIn("另有 2 条内容，见完整一期。", latest)
            self.assertIn(long_zh, latest)
            self.assertIn(long_en.strip(), latest)
            self.assertNotIn(long_en.strip() + " ", latest)
            self.assertLess(
                latest.index("第一条不是头条"),
                latest.index(long_en.strip()),
            )
            inner = (site / "editions" / "2026-08-02" / "index.html").read_text(encoding="utf-8")
            self.assertIn('id="event-2"', inner)
            self.assertIn(escape_title(malicious_title), inner)
            self.assertLess(archive.index("2026 年 8 月"), archive.index("2025 年 12 月"))
            self.assertIn("08 月 01 日", archive)
            self.assertIn("只有头条", archive)
            self.assertIn("1 条内容", archive)
            self.assertIn("12 月 31 日", archive)
            self.assertIn("无显式头条的第一条", archive)
            self.assertIn("3 条内容", archive)
            self.assertIn("此前已刊 2 期", archive)
            self.assertNotIn(secret, (site / "index.html").read_text(encoding="utf-8"))
            published = json.loads(
                (site / "editions" / "2026-08-02" / "edition.json").read_text(encoding="utf-8")
            )
            self.assertEqual(published["sources"][1]["error"], secret)

            only = make_edition(
                "2026-08-03",
                [make_event("solo", "单独头条", placement="lead")],
                cutoff_at="2026-08-03T08:00:00+08:00",
            )
            short = make_edition(
                "2026-08-04",
                [
                    make_event("s0", "不足三条之一", placement="lead", conditions=["条件甲"]),
                    make_event("s1", "不足三条之二", placement="story"),
                    make_event("s2", "不足三条之三", placement="story"),
                ],
                cutoff_at="2026-08-04T08:00:00+08:00",
            )
            no_lead = make_edition(
                "2026-08-05",
                [
                    make_event("n0", "默认第一条当头条", placement="story"),
                    make_event("n1", "默认第二条", placement="story"),
                ],
                cutoff_at="2026-08-05T08:00:00+08:00",
            )
            isolated = root / "isolated"
            publish(isolated, only)
            built_only = root / "built-only"
            build_site(isolated, built_only)
            only_latest = region(
                (built_only / "index.html").read_text(encoding="utf-8"),
                "latest",
                "archive",
            )
            self.assertIn("lead-layout--single", only_latest)
            self.assertNotIn("本期还包括", only_latest)
            self.assertNotIn("另有", only_latest)
            self.assertNotIn('class="scope"', only_latest)

            two = root / "two"
            publish(two, short)
            built_two = root / "built-two"
            build_site(two, built_two)
            two_latest = region(
                (built_two / "index.html").read_text(encoding="utf-8"),
                "latest",
                "archive",
            )
            self.assertNotIn("lead-layout--single", two_latest)
            self.assertIn("不足三条之二", two_latest)
            self.assertIn("不足三条之三", two_latest)
            self.assertIn('href="editions/2026-08-04/#event-2"', two_latest)
            self.assertIn('href="editions/2026-08-04/#event-3"', two_latest)
            self.assertNotIn("另有", two_latest)
            self.assertIn("条件甲", two_latest)
            self.assertIn('class="scope"', two_latest)

            implicit = root / "implicit"
            publish(implicit, no_lead)
            built_implicit = root / "built-implicit"
            build_site(implicit, built_implicit)
            implicit_html = (built_implicit / "index.html").read_text(encoding="utf-8")
            implicit_latest = region(implicit_html, "latest", "archive")
            self.assertIn("默认第一条当头条", implicit_latest)
            self.assertIn('href="editions/2026-08-05/#event-2"', implicit_latest)
            self.assertNotIn('href="editions/2026-08-05/#event-1"', implicit_latest)
            inner_implicit = (
                built_implicit / "editions" / "2026-08-05" / "index.html"
            ).read_text(encoding="utf-8")
            self.assertIn('id="event-1"', inner_implicit)
            self.assertLess(
                inner_implicit.index("默认第一条当头条"),
                inner_implicit.index('id="event-2"'),
            )

            poisoned = make_edition(
                "2026-04-01",
                [make_event(
                    "poison",
                    "占位符{{ARCHIVE}}",
                    placement="lead",
                    fact="摘录里的{{DESCRIPTION}}不应重写模板",
                )],
                cutoff_at="2026-04-01T08:00:00+08:00",
            )
            poisoned_html = _history_index([poisoned])
            self.assertIn("占位符{{ARCHIVE}}", poisoned_html)
            self.assertIn("摘录里的{{DESCRIPTION}}不应重写模板", poisoned_html)
            self.assertEqual(poisoned_html.count('id="archive"'), 1)
            self.assertIn("AI 日报 · 最新一期与往期", poisoned_html)

    def test_continuous_publication_switches_every_homepage_field_from_a_to_b(self):
        edition_a = make_edition(
            "2026-09-16",
            [
                make_event(
                    "a-lead",
                    "甲头条标题",
                    placement="lead",
                    kicker="甲类",
                    fact="甲事实完整段落称可能尚未。",
                    conditions=["甲条件一", "甲条件二"],
                ),
                make_event("a-side", "甲侧栏唯一", placement="story", kicker="甲侧"),
            ],
            cutoff_at="2026-09-16T14:00:00+08:00",
        )
        edition_b = make_edition(
            "2026-09-18",
            [
                make_event(
                    "b-lead",
                    "乙头条标题",
                    placement="lead",
                    kicker="乙类",
                    fact="乙事实完整段落称可能尚未。",
                    conditions=["乙条件仅此"],
                ),
                make_event("b2", "乙侧栏一", placement="story"),
                make_event("b3", "乙侧栏二", placement="story"),
                make_event("b4", "乙侧栏三", placement="story"),
                make_event("b5", "乙近期选读", placement="story", window="recent"),
            ],
            cutoff_at="2026-09-18T08:30:00+08:00",
            sources=[
                {"id": "official", "status": "success", "item_count": 4, "error": None},
                {
                    "id": "broken",
                    "status": "failed",
                    "item_count": 0,
                    "error": "乙来源失败原文不得上首页",
                },
            ],
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            editions = root / "editions"
            site = root / "site"
            publish(editions, edition_a)
            digest_a = file_digests(editions)
            build_site(editions, site)
            self.assertEqual(file_digests(editions), digest_a)
            first = (site / "index.html").read_text(encoding="utf-8")
            first_latest = region(first, "latest", "archive")
            self.assertIn("2026.09.16", first_latest)
            self.assertIn(weekday_label("2026-09-16"), first_latest)
            self.assertIn(coverage_note(edition_a), first_latest)
            self.assertIn("甲类", first_latest)
            self.assertIn("甲头条标题", first_latest)
            self.assertIn("甲事实完整段落称可能尚未。", first_latest)
            self.assertIn("甲条件一", first_latest)
            self.assertIn("甲条件二", first_latest)
            self.assertIn('class="read-edition" href="editions/2026-09-16/"', first_latest)
            self.assertIn("甲侧栏唯一", first_latest)
            self.assertIn('href="editions/2026-09-16/#event-2"', first_latest)
            self.assertIn("2 条内容", first_latest)
            self.assertNotIn("另有", first_latest)
            self.assertIn("暂无更早期次", region(first, "archive", "principles"))
            self.assertNotIn("2026-09-18", first)

            repeated = (site / "index.html").read_bytes()
            webp = hashlib.sha256(
                (site / "assets" / "masthead-art.webp").read_bytes()
            ).hexdigest()
            build_site(editions, site)
            self.assertEqual((site / "index.html").read_bytes(), repeated)
            self.assertEqual(
                hashlib.sha256((site / "assets" / "masthead-art.webp").read_bytes()).hexdigest(),
                webp,
            )
            self.assertEqual(webp, APPROVED_MASTHEAD_SHA256)
            self.assertEqual(file_digests(editions), digest_a)

            publish(editions, edition_b)
            digest_ab = file_digests(editions)
            self.assertEqual(
                digest_ab["2026-09-16/edition.json"],
                digest_a["2026-09-16/edition.json"],
            )
            self.assertEqual(
                digest_ab["2026-09-16/index.html"],
                digest_a["2026-09-16/index.html"],
            )
            build_site(editions, site)
            self.assertEqual(file_digests(editions), digest_ab)
            updated = (site / "index.html").read_text(encoding="utf-8")
            latest = region(updated, "latest", "archive")
            archive = region(updated, "archive", "principles")
            head = updated.split("<body", 1)[0]
            self.assertIn("2026.09.18", latest)
            self.assertIn(weekday_label("2026-09-18"), latest)
            self.assertNotEqual(weekday_label("2026-09-16"), weekday_label("2026-09-18"))
            self.assertIn(coverage_note(edition_b), latest)
            self.assertIn("4 条窗口内动态", latest)
            self.assertIn("另附 1 条近期选读", latest)
            self.assertIn("另有 1 个来源采集失败", latest)
            self.assertIn("乙类", latest)
            self.assertIn("乙头条标题", latest)
            self.assertIn("乙事实完整段落称可能尚未。", latest)
            self.assertNotIn("第二段不应进入首页摘录。", latest)
            self.assertIn("乙条件仅此", latest)
            self.assertIn('class="read-edition" href="editions/2026-09-18/"', latest)
            self.assertIn('href="editions/2026-09-18/#event-2"', latest)
            self.assertIn('href="editions/2026-09-18/#event-3"', latest)
            self.assertIn('href="editions/2026-09-18/#event-4"', latest)
            self.assertNotIn("editions/2026-09-18/#event-5", latest)
            self.assertIn("乙侧栏一", latest)
            self.assertIn("乙侧栏二", latest)
            self.assertIn("乙侧栏三", latest)
            self.assertNotIn("乙近期选读", latest)
            self.assertIn("5 条内容", latest)
            self.assertIn("另有 1 条内容，见完整一期。", latest)
            self.assertNotIn("甲头条标题", latest)
            self.assertNotIn("甲事实完整段落称可能尚未。", latest)
            self.assertIn("2026 年 9 月 18 日", head)
            self.assertIn("共 5 条内容", head)
            self.assertNotIn("2026 年 9 月 16 日", head)
            self.assertNotIn("乙来源失败原文不得上首页", updated)
            self.assertIn("09 月 16 日", archive)
            self.assertIn("甲头条标题", archive)
            self.assertIn("2 条内容", archive)
            self.assertIn('href="editions/2026-09-16/"', archive)
            self.assertIn("2026 年 9 月", archive)
            self.assertIn("此前已刊 1 期", archive)
            self.assertNotIn("乙头条标题", archive)

            again = (site / "index.html").read_bytes()
            build_site(editions, site)
            self.assertEqual((site / "index.html").read_bytes(), again)
            self.assertNotIn("2099-01-01", (site / "index.html").read_text(encoding="utf-8"))
            self.assertEqual(file_digests(editions), digest_ab)

            bad = editions / "2026-09-19"
            bad.mkdir()
            (bad / "edition.json").write_text("{}\n", encoding="utf-8")
            (bad / "index.html").write_text("not a render\n", encoding="utf-8")
            digest_with_bad = file_digests(editions)
            with self.assertRaises(EditionError):
                build_site(editions, site)
            self.assertEqual((site / "index.html").read_bytes(), again)
            self.assertEqual(file_digests(editions), digest_with_bad)
            self.assertIn("乙头条标题", (site / "index.html").read_text(encoding="utf-8"))


def escape_title(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


if __name__ == "__main__":
    unittest.main()
