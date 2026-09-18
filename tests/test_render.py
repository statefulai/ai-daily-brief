import tempfile
import unittest
from pathlib import Path

from edition import EditionError, build_edition, write_edition
from outputs import (
    build_email_send_parts,
    render_email_html,
    render_email_text,
    render_group_message,
    render_web_edition,
    require_full_email_html,
    write_web_edition,
)


def event(
    index: int,
    placement: str = "story",
    window: str = "in_window",
    conditions: list[str] | None = None,
    note: str | None = None,
) -> dict:
    if conditions is None:
        conditions = ["仅适用于已开放账号。"] if index == 1 else []
    return {
        "id": f"event-{index}",
        "placement": placement,
        "window": window,
        "title": f"第 {index} 条新闻 <script>alert(1)</script>",
        "kicker": "官方更新",
        "facts": [f"第 {index} 条核心事实。"],
        "conditions": conditions,
        "background": ["补充背景。"] if index == 1 else [],
        "sources": [
            {
                "label": "官方来源",
                "url": f"https://example.com/{index}",
                "kind": "primary",
                "published_at": f"2026-09-{15 if window == 'recent' else 16:02d}T01:00:00+08:00",
                "verified": True,
                "note": note,
            }
        ],
    }


def edition(events: list[dict]) -> dict:
    return build_edition(
        edition_id="2026-09-16",
        cutoff_at="2026-09-16T14:00:00+08:00",
        sources=[{"id": "official", "status": "success", "item_count": len(events), "error": None}],
        events=events,
        generated_at="2026-09-16T14:05:00+08:00",
    )


class RenderTest(unittest.TestCase):
    def test_short_edition_omits_empty_columns(self):
        html = render_web_edition(edition([event(1, "lead")]))

        self.assertNotIn('id="news"', html)
        self.assertNotIn('id="practice"', html)
        self.assertIn("今日焦点", html)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)

    def test_regular_edition_renders_both_optional_columns(self):
        html = render_web_edition(
            edition([event(1, "lead"), event(2), event(3, "desk", "recent")])
        )

        self.assertIn('id="news"', html)
        self.assertIn('id="practice"', html)
        self.assertIn("实操与问答", html)
        self.assertIn("近期选读", html)
        self.assertNotIn("近期选读</time><span>近期选读", html)
        self.assertIn("<details>", html)
        self.assertIn('<p class="condition"><strong>适用范围：</strong>', html)
        self.assertNotIn("适用条件：", html)
        self.assertNotIn("。；", html)
        self.assertNotIn('<div class="condition">', html)
        self.assertIn(".lead .kicker,.story .kicker,.desk-story .kicker { justify-content:center; }", html)
        self.assertIn(".story h3,.desk-story h3 { margin:0 auto 13px; text-align:center; text-wrap:balance;", html)
        self.assertIn(".lead .kicker,.story .kicker,.desk-story .kicker { justify-content:flex-start; }", html)
        self.assertNotIn("edition.json", html)
        self.assertNotIn("sha256:", html)

    def test_many_events_keep_one_document(self):
        events = [event(1, "lead")] + [event(index) for index in range(2, 13)]
        html = render_web_edition(edition(events))

        self.assertEqual(html.count('class="story"'), 11)
        self.assertNotIn("pagination", html.lower())

    def test_email_and_group_outputs_share_the_same_edition(self):
        document = edition([event(1, "lead"), event(2), event(3), event(4)])
        public_url = "https://example.com/editions/2026-09-16/"

        email_html = render_email_html(document, public_url)
        email_text = render_email_text(document, public_url)
        group = render_group_message(document, public_url)
        send_parts = build_email_send_parts(document, public_url)

        self.assertIn("查看网页版", email_html)
        self.assertIn("font-family:'Songti SC',serif", email_html)
        self.assertNotIn('font:700 24px/1.45 "Songti SC"', email_html)
        self.assertIn("font-family:Didot,'Bodoni 72','Times New Roman',serif", email_html)
        self.assertIn('font-weight:400;letter-spacing:.035em;">AI</td>', email_html)
        self.assertIn('font-weight:400;letter-spacing:.04em;">日报</td>', email_html)
        self.assertNotIn('font:700 42px/1.3', email_html)
        self.assertNotIn("edition.json", email_html)
        self.assertNotIn("content_hash", email_html)
        self.assertNotIn("sha256:", email_html)
        self.assertIn("网页版：https://example.com/editions/2026-09-16/", email_text)
        self.assertIn("适用范围：仅适用于已开放账号。", email_text)
        self.assertIn('<p class="condition"><strong>适用范围：</strong>仅适用于已开放账号。</p>', render_web_edition(document))
        self.assertIn("<strong>适用范围：</strong>仅适用于已开放账号。", email_html)
        self.assertIn("适用范围：仅适用于已开放账号。", group)
        self.assertNotIn("适用条件：", email_html)
        self.assertNotIn("适用条件：", email_text)
        self.assertNotIn("适用条件：", group)
        self.assertNotIn("。；", email_html)
        self.assertNotIn("。；", email_text)
        self.assertNotIn("另有 1 条，详见网页版。", group)
        self.assertEqual(send_parts["htmlBody"], email_html)
        self.assertEqual(send_parts["body"], email_text)
        self.assertEqual(group, f"【AI 日报｜2026-09-16】{email_text[len('AI 日报｜2026-09-16'):]}")
        for index in range(1, 5):
            title = f"第 {index} 条新闻 <script>alert(1)</script>"
            fact = f"第 {index} 条核心事实。"
            self.assertIn(title, email_html)
            self.assertIn(fact, email_html)
            self.assertIn(title, email_text)
            self.assertIn(fact, email_text)
            self.assertIn(title, group)
            self.assertIn(fact, group)
            self.assertIn(title, send_parts["htmlBody"])
            self.assertIn(fact, send_parts["body"])
        self.assertNotIn("<section", group)
        self.assertNotIn("<h2", group)
        self.assertNotIn("style=", group)
        self.assertNotIn("<img", email_html)
        self.assertNotIn("附件", email_html)

    def test_email_send_parts_reject_summary_card_html(self):
        document = edition([event(1, "lead"), event(2), event(3), event(4)])
        public_url = "https://example.com/editions/2026-09-16/"
        card = (
            "<html><body><h1>AI 日报</h1><ul>"
            "<li>第 1 条新闻</li><li>第 2 条新闻</li><li>第 3 条新闻</li>"
            "</ul><p>另有 1 条，详见网页版。</p>"
            f'<a href="{public_url}">查看网页版</a></body></html>'
        )

        with self.assertRaisesRegex(EditionError, "complete email.html"):
            require_full_email_html(card, document, public_url)
        parts = build_email_send_parts(document, public_url)
        self.assertEqual(parts["htmlBody"], render_email_html(document, public_url))
        self.assertIn("第 4 条核心事实。", parts["htmlBody"])
        self.assertIn("第 4 条核心事实。", parts["body"])

    def test_multi_item_ranges_render_per_item(self):
        document = edition(
            [
                event(
                    1,
                    "lead",
                    conditions=[
                        "适用于付费计划。",
                        "目前处于公开预览。",
                    ],
                    note="仅确认到日期。",
                )
            ]
        )
        public_url = "https://example.com/editions/2026-09-16/"
        html = render_web_edition(document)
        email_html = render_email_html(document, public_url)
        email_text = render_email_text(document, public_url)
        group = render_group_message(document, public_url)

        self.assertIn('<div class="condition"><strong>适用范围：</strong><ul><li>适用于付费计划。</li>', html)
        self.assertIn("<li>目前处于公开预览。</li></ul></div>", html)
        self.assertIn("核验说明（官方来源）：仅确认到日期。", html)
        self.assertIn("<ul style=\"margin:6px 0 0;padding-left:20px;\"><li>适用于付费计划。</li>", email_html)
        self.assertIn("核验说明（官方来源）：仅确认到日期。", email_html)
        self.assertEqual(
            [line for line in email_text.splitlines() if line.startswith("适用范围") or "付费" in line or "预览" in line or line.startswith("核验")],
            [
                "适用范围：",
                "适用于付费计划。",
                "目前处于公开预览。",
                "核验说明（官方来源）：仅确认到日期。",
            ],
        )
        self.assertIn("适用范围：", group)
        self.assertIn("适用于付费计划。", group)
        self.assertIn("目前处于公开预览。", group)
        self.assertNotIn("适用条件：", group)
        self.assertEqual(
            group,
            f"【AI 日报｜2026-09-16】{email_text[len('AI 日报｜2026-09-16'):]}",
        )
        self.assertNotIn("详见网页版。", group)
        self.assertNotIn("。；", html)
        self.assertNotIn("。；", email_html)
        self.assertNotIn("。；", email_text)
        self.assertNotIn("适用条件：", html)

    def test_production_directory_contains_only_json_and_html(self):
        document = edition([event(1, "lead")])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_edition(root, document)
            write_web_edition(root, document)
            names = {path.name for path in (root / "2026-09-16").iterdir()}

        self.assertEqual(names, {"edition.json", "index.html"})


if __name__ == "__main__":
    unittest.main()
