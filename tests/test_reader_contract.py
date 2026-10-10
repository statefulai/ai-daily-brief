import json
import unittest
from pathlib import Path

from edition import (
    EditionError,
    content_hash,
    reader_contract_warnings,
    strip_leading_time_metadata,
    text_has_time_metadata,
    validate_edition,
    visible_char_count,
)
from outputs import (
    edition_public_url,
    render_email_html,
    render_email_text,
    render_group_message,
    render_web_edition,
    require_full_email_html,
)
from scripts.edition_ci import _latest_html


def source(note: str | None = "核验说明仍保留。datePublished 是 2026-10-09T16:09:00.000Z。") -> dict:
    return {
        "label": "官方",
        "url": "https://example.com/a",
        "kind": "primary",
        "published_at": "2026-10-10T00:00:00+08:00",
        "verified": True,
        "note": note,
    }


def reader_event(event_id: str = "item", lede: str = "甲" * 80, **extra) -> dict:
    event = {
        "id": event_id,
        "placement": "story",
        "window": "in_window",
        "title": f"标题 {event_id}",
        "kicker": "分类",
        "facts": ["一条很长的核验事实，不会因为有了正文就被删掉。" * 8],
        "conditions": ["适用范围仍保留，不拿来当限定。"],
        "background": ["背景仍保留。"],
        "sources": [source()],
        "lede": lede,
    }
    event.update(extra)
    return event


def wrap(events: list[dict], edition_id: str = "2026-10-11") -> dict:
    return {
        "schema_version": "1",
        "edition_id": edition_id,
        "timezone": "Asia/Shanghai",
        "cutoff_at": f"{edition_id}T08:00:00+08:00",
        "generated_at": f"{edition_id}T08:01:00+08:00",
        "attempt": 1,
        "status": "published_candidate",
        "sources": [{"id": "official", "status": "success", "item_count": 1, "error": None}],
        "events": events,
        "contribution_count": 0,
        "failure": None,
    }


class ReaderContractTest(unittest.TestCase):
    def test_lede_length_fails_above_hard_limits(self):
        with self.assertRaisesRegex(EditionError, "exceeds 200"):
            validate_edition(wrap([reader_event(lede="甲" * 201)]))
        with self.assertRaisesRegex(EditionError, "without length_exception"):
            validate_edition(wrap([reader_event(lede="甲" * 121)]))

    def test_lede_length_passes_when_links_are_excluded(self):
        lede = "甲" * 80 + " https://example.com/docs/very/long/path "
        self.assertEqual(visible_char_count(lede), 80)
        caveat = "限" * 41
        edition = validate_edition(wrap([reader_event(lede=lede, caveat=caveat)]))

        self.assertEqual(visible_char_count(edition["events"][0]["lede"]), 80)
        self.assertLessEqual(visible_char_count(edition["events"][0]["lede"]), 120)
        self.assertEqual(
            reader_contract_warnings(edition),
            ["item: caveat is 41 characters"],
        )
        self.assertEqual(edition["events"][0]["facts"][0][:4], "一条很长")
        self.assertEqual(edition["events"][0]["conditions"], ["适用范围仍保留，不拿来当限定。"])

    def test_length_exception_over_two_fails(self):
        events = [
            reader_event(
                f"e{index}",
                lede="乙" * 150,
                length_exception="政策全文和多方说法需要写进同一段",
            )
            for index in range(3)
        ]
        with self.assertRaisesRegex(EditionError, "at most 2"):
            validate_edition(wrap(events))

    def test_two_length_exceptions_pass_with_warning(self):
        events = [
            reader_event(
                f"e{index}",
                lede="乙" * 150,
                length_exception="政策全文和多方说法需要写进同一段",
            )
            for index in range(2)
        ]
        edition = validate_edition(wrap(events))
        warnings = reader_contract_warnings(edition)
        self.assertEqual(
            warnings,
            [
                "e0: lede is 150 characters with length_exception",
                "e1: lede is 150 characters with length_exception",
            ],
        )
        self.assertEqual(visible_char_count(edition["events"][0]["lede"]), 150)

    def test_reader_time_metadata_fails(self):
        cases = [
            {"lede": "甲" * 20 + "datePublished"},
            {"lede": "甲" * 20 + "dateModified"},
            {"lede": "甲" * 20 + "2026-10-09T16:09:00.000Z"},
            {"lede": "甲" * 20 + "2026-10-09T18:35:51+00:00"},
            {"lede": "甲" * 20 + "2026-10-09T16:09:00"},
            {"lede": "甲" * 20 + "2026-10-09T16:09"},
            {"lede": "甲" * 20 + "2026-10-09 16:09:00"},
            {"lede": "甲" * 20 + "16:09+0800"},
            {"lede": "甲" * 20 + "16:09 +0800"},
            {"lede": "甲" * 20 + "16:09 +08:00"},
            {"lede": "甲" * 20 + "16:09 UTC+8"},
            {"lede": "甲" * 20 + "16:09 GMT+0800"},
            {"lede": "甲" * 20 + "16:09Z"},
            {"lede": "甲" * 20 + "16:09:00.000Z"},
            {"lede": "甲" * 20 + "16:09+08"},
            {"lede": "甲" * 20 + "20:30-0700"},
            {"lede": "甲" * 20 + "2026/10/09 16:09"},
            {"lede": "甲" * 20 + "T16:09:00 UTC"},
            {"lede": "甲" * 20 + "DatePublished"},
            {"lede": "甲" * 30, "caveat": "页面 lastmod 刚更新"},
            {"lede": "甲" * 30, "caveat": "createdAt 不该出现"},
            {"lede": "甲" * 30, "caveat": "时区写成 16:09:00+08:00"},
            {"lede": "甲" * 30, "caveat": "2026-10-09T16:09:00"},
            {"lede": "甲" * 30, "caveat": "DatePublished"},
        ]
        for extra in cases:
            with self.subTest(extra=extra):
                with self.assertRaisesRegex(EditionError, "time metadata"):
                    validate_edition(wrap([reader_event(**extra)]))

    def test_lede_length_boundaries_ignore_whitespace(self):
        exact_120 = "甲" * 120
        spaced = "甲 \n" * 120
        self.assertEqual(visible_char_count(exact_120), 120)
        self.assertEqual(visible_char_count(spaced), visible_char_count(exact_120))
        for event_id, lede in (("exact120", exact_120), ("spaced120", spaced)):
            edition = validate_edition(wrap([reader_event(event_id, lede=lede)]))
            self.assertEqual(visible_char_count(edition["events"][0]["lede"]), 120)
            self.assertEqual(reader_contract_warnings(edition), [])

        exact_200 = "乙" * 200
        edition = validate_edition(
            wrap([
                reader_event(
                    "exact200",
                    lede=exact_200,
                    length_exception="政策全文需要把关键条款写进正文",
                )
            ])
        )
        self.assertEqual(visible_char_count(edition["events"][0]["lede"]), 200)
        self.assertEqual(
            reader_contract_warnings(edition),
            ["exact200: lede is 200 characters with length_exception"],
        )

    def test_caveat_without_lede_fails(self):
        event = reader_event()
        del event["lede"]
        event["caveat"] = "还没上线"
        with self.assertRaisesRegex(EditionError, "lede is required"):
            validate_edition(wrap([event]))

    def test_plain_date_and_numbers_are_not_time_metadata(self):
        lede = "公司于 2026-10-09 公布，价格 0.042 美元，另有 1000 个名额，10 月 9 日可以申请。"
        caveat = "10 月 9 日上线"
        edition = validate_edition(wrap([reader_event(lede=lede, caveat=caveat)]))
        self.assertEqual(reader_contract_warnings(edition), [])
        self.assertIn("2026-10-09", edition["events"][0]["lede"])
        self.assertIn("0.042", edition["events"][0]["lede"])
        self.assertFalse(text_has_time_metadata("2026-10-09"))
        self.assertFalse(text_has_time_metadata("2026/10/09"))
        self.assertFalse(text_has_time_metadata("10 月 9 日"))

    def test_signed_amounts_scores_and_bare_clock_utc_pass(self):
        phrases = (
            "营收 +1500 万",
            "裁员 -1200 人",
            "gpt-4o-2024-08-06",
            "比分 +12:30",
            "17:00 UTC 起生效",
        )
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                self.assertFalse(text_has_time_metadata(phrase))
        lede = "甲" * 20 + "。".join(phrases)
        edition = validate_edition(wrap([reader_event(lede=lede)]))
        self.assertEqual(reader_contract_warnings(edition), [])
        self.assertFalse(text_has_time_metadata(edition["events"][0]["lede"]))
        ranges = (
            "14:00-16:00",
            "09:00-18:00",
            "08:30-11:30",
            "22:00-06:00",
            "14:00-16",
            "15:00 -12 点",
            "16:09 Z世代用户",
            "16:09 Z轴",
            "10:30 z轴",
            "会议 14:00-16:00",
            "09:00-17:00 开放",
        )
        for phrase in ranges:
            with self.subTest(phrase=phrase):
                self.assertFalse(text_has_time_metadata(phrase))
                ranged = validate_edition(wrap([reader_event(lede="甲" * 20 + phrase)]))
                self.assertEqual(reader_contract_warnings(ranged), [])

    def test_reader_links_follow_public_url_once(self):
        document = wrap([reader_event(lede="甲" * 80, caveat="还没上线")])
        public_url = "https://example.com/editions/2026-10-11/"
        text = render_email_text(document, public_url)
        email = render_email_html(document, public_url)
        self.assertIn("网页版：https://example.com/editions/2026-10-11/", text)
        self.assertIn("首页：https://example.com/", text)
        self.assertEqual(text.count("网页版："), 1)
        self.assertNotIn("brief.sanze.dev", text)
        self.assertNotIn("brief.sanze.dev", email)
        self.assertEqual(email.count("查看网页版"), 1)

    def test_reader_plain_date_passes(self):
        lede = "Anthropic 10 月 9 日披露，其模型在评估里出现非预期动作，公司决定内部评估断开实时互联网。"
        caveat = "断网只限内部评估"
        edition = validate_edition(wrap([reader_event(lede=lede, caveat=caveat)]))

        self.assertEqual(reader_contract_warnings(edition), [])
        self.assertIn("10 月 9 日", edition["events"][0]["lede"])
        self.assertIn("datePublished", edition["events"][0]["sources"][0]["note"])
        self.assertLessEqual(visible_char_count(lede), 120)
        self.assertLessEqual(visible_char_count(caveat), 40)

    def test_legacy_edition_without_reader_fields_passes(self):
        fact = (
            "Anthropic 研究页的 datePublished 是 2026-10-09T16:09:00.000Z。"
            + "核验事实继续保留。" * 20
        )
        self.assertGreater(visible_char_count(fact), 200)
        event = reader_event(lede="占位")
        del event["lede"]
        event["facts"] = [fact, "第二段事实也保留。"]
        document = wrap([event])
        edition = validate_edition(document)
        again = validate_edition(edition)

        self.assertEqual(content_hash(edition), content_hash(again))
        self.assertNotIn("lede", edition["events"][0])
        self.assertNotIn("caveat", edition["events"][0])
        self.assertEqual(reader_contract_warnings(edition), [])
        web = render_web_edition(edition)
        email = render_email_html(edition, "https://example.com/editions/2026-10-11/")
        text = render_email_text(edition, "https://example.com/editions/2026-10-11/")
        self.assertIn(fact, web)
        self.assertIn("适用范围：", web)
        self.assertIn("核验说明（官方）", web)
        self.assertNotIn("详细与核验", web)
        self.assertIn(fact, email)
        self.assertIn("适用范围仍保留，不拿来当限定。", email)
        self.assertIn("核验说明（官方）", text)
        self.assertNotIn("https://brief.sanze.dev/", email)
        self.assertIn("网页版：https://example.com/editions/2026-10-11/", text)

    def test_reader_fields_do_not_replace_required_facts(self):
        event = reader_event()
        event["facts"] = []
        with self.assertRaisesRegex(EditionError, "facts must not be empty"):
            validate_edition(wrap([event]))

    def test_reader_mail_hides_verification_blocks(self):
        lede = "甲" * 90
        caveat = "数字是公司自测"
        long_fact = "这条核验事实只应出现在网页的详细与核验里，邮件不展开。" * 3
        event = reader_event(
            lede=lede,
            caveat=caveat,
            facts=[long_fact],
            conditions=["只限已开通的企业账号。"],
        )
        event["sources"].append(
            {
                "label": "补充",
                "url": "https://example.com/secondary",
                "kind": "secondary",
                "published_at": "2026-10-10T01:00:00+08:00",
                "verified": True,
                "note": "补充来源的核验说明。",
            }
        )
        document = wrap([event])
        public_url = edition_public_url("2026-10-11")
        web = render_web_edition(document)
        email = render_email_html(document, public_url)
        text = render_email_text(document, public_url)
        group = render_group_message(document, public_url)
        require_full_email_html(email, document, public_url)

        self.assertIn("<details><summary>详细与核验</summary>", web)
        self.assertNotIn("<details open", web)
        self.assertIn(long_fact, web)
        self.assertIn("适用范围：", web)
        self.assertIn("核验说明（官方）", web)
        self.assertIn("补充来源的核验说明。", web)
        self.assertIn(lede, email)
        self.assertIn("限定：数字是公司自测", email)
        self.assertNotIn(long_fact, email)
        self.assertNotIn("适用范围", email)
        self.assertNotIn("核验说明", email)
        self.assertNotIn("https://example.com/secondary", email)
        self.assertNotIn(long_fact, text)
        self.assertNotIn("https://example.com/secondary", text)
        self.assertNotIn("核验说明", group)
        self.assertIn("原文：https://example.com/a", text)
        self.assertEqual(text.count("https://example.com/a"), 1)
        self.assertIn("网页版：https://brief.sanze.dev/editions/2026-10-11/", text)
        self.assertIn("首页：https://brief.sanze.dev/", text)
        self.assertIn("截至 10 月 11 日 08:00", text)
        self.assertEqual(group, f"【AI 日报｜2026-10-11】{text[len('AI 日报｜2026-10-11'):]}")

    def test_homepage_excerpt_drops_leading_timestamps_and_clamps_three_lines(self):
        fact = (
            "Anthropic 研究页的 datePublished 是 2026-10-09T16:09:00.000Z，"
            "dateModified 是 2026-10-09T22:04:00.000Z，可见日期写 Oct 9, 2026。"
            "这篇报告写评估里出现非预期动作。"
        )
        excerpt = strip_leading_time_metadata(fact)
        self.assertTrue(excerpt.startswith("这篇报告写"))
        self.assertNotIn("datePublished", excerpt)
        self.assertEqual(
            strip_leading_time_metadata("甲事实完整段落称可能尚未。"),
            "甲事实完整段落称可能尚未。",
        )
        event = reader_event("lead", lede="占位")
        del event["lede"]
        event["placement"] = "lead"
        event["facts"] = [fact]
        edition = validate_edition(wrap([event], "2026-10-10"))
        latest = _latest_html(edition)
        self.assertIn("这篇报告写评估里出现非预期动作。", latest)
        self.assertNotIn("datePublished", latest)
        self.assertNotIn("2026-10-09T16:09:00.000Z", latest)
        template = Path("templates/home.html").read_text(encoding="utf-8")
        self.assertIn("-webkit-line-clamp: 3", template)
        self.assertIn("line-clamp: 3", template)

    def test_published_editions_render_and_hash_stay_byte_identical(self):
        root = Path("editions")
        checked = []
        for directory in sorted(path for path in root.iterdir() if path.is_dir()):
            if not ("2026-09-17" <= directory.name <= "2026-10-10"):
                continue
            document = json.loads((directory / "edition.json").read_text(encoding="utf-8"))
            edition = validate_edition(document)
            self.assertEqual(document["content_hash"], edition["content_hash"], directory.name)
            self.assertEqual(
                (directory / "index.html").read_text(encoding="utf-8"),
                render_web_edition(document),
                directory.name,
            )
            for event in edition["events"]:
                self.assertNotIn("lede", event)
            checked.append(directory.name)
        self.assertGreaterEqual(len(checked), 20)

    def test_sample_edition_keeps_verification_data_and_slim_channels(self):
        original = json.loads(Path("editions/2026-10-10/edition.json").read_text(encoding="utf-8"))
        sample_dir = Path("fixtures/2026-10-10-reader")
        sample = json.loads((sample_dir / "edition.json").read_text(encoding="utf-8"))
        edition = validate_edition(sample)
        self.assertEqual([event["id"] for event in edition["events"]], [event["id"] for event in original["events"]])
        exceptions = 0
        for old, new in zip(original["events"], edition["events"]):
            self.assertEqual(new["facts"], old["facts"])
            self.assertEqual(new["conditions"], old["conditions"])
            self.assertEqual(new["background"], old["background"])
            self.assertEqual(new["sources"], old["sources"])
            self.assertLessEqual(visible_char_count(new["lede"]), 200)
            self.assertGreaterEqual(visible_char_count(new["lede"]), 80)
            if visible_char_count(new["lede"]) > 120:
                self.assertTrue(new.get("length_exception"))
                exceptions += 1
            else:
                self.assertNotIn("length_exception", new)
            self.assertLessEqual(visible_char_count(new["caveat"]), 40)
        self.assertLessEqual(exceptions, 2)
        self.assertGreaterEqual(exceptions, 1)

        public_url = edition_public_url("2026-10-10")
        web = render_web_edition(sample)
        email = render_email_html(sample, public_url)
        text = render_email_text(sample, public_url)
        group = render_group_message(sample, public_url)
        self.assertEqual((sample_dir / "web.html").read_text(encoding="utf-8"), web)
        self.assertEqual((sample_dir / "email.html").read_text(encoding="utf-8"), email)
        self.assertEqual((sample_dir / "group.txt").read_text(encoding="utf-8"), group)
        self.assertEqual(web.count("<details><summary>详细与核验</summary>"), 7)
        self.assertNotIn("<details open", web)
        self.assertIn("适用范围", web)
        self.assertIn("核验说明", web)
        self.assertIn("datePublished", web)
        self.assertIn("10 个来源采集失败", email)
        self.assertIn("2026 年 10 月 10 日", email)
        self.assertIn("星期六", email)
        self.assertIn("查看网页版", email)
        self.assertIn("https://brief.sanze.dev/editions/2026-10-10/", email)
        self.assertIn("https://brief.sanze.dev/", email)
        self.assertNotIn("适用范围", email)
        self.assertNotIn("核验说明", email)
        self.assertNotIn("datePublished", email)
        self.assertNotIn("适用范围", group)
        self.assertNotIn("核验说明", group)
        self.assertIn("首页：https://brief.sanze.dev/", group)
        self.assertIn("截至 10 月 10 日 08:00（北京时间）。本期收录 7 条窗口内动态", group)
        for event in edition["events"]:
            self.assertIn(event["lede"], email)
            self.assertIn(event["caveat"], email)
            self.assertNotIn(event["facts"][0], email)
            self.assertNotIn(event["conditions"][0], email)
            self.assertIn(event["facts"][0], web)
            primary = next(item["url"] for item in event["sources"] if item["kind"] == "primary")
            self.assertEqual(text.count(primary), 1)
            for item in event["sources"]:
                if item["url"] != primary:
                    self.assertNotIn(item["url"], text)
                    self.assertNotIn(item["url"], email)


if __name__ == "__main__":
    unittest.main()
