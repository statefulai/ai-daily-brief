import json
import tempfile
import threading
import unittest
from pathlib import Path

from delivery.local_dogfood import render_dogfood_page
from delivery.payload import build_email_send_parts, require_full_email_html
from delivery.state import DeliveryKey, DeliveryLedger
from edition import EditionError, build_edition
from outputs import render_email_html, render_email_text, render_group_message


def document() -> dict:
    return build_edition(
        edition_id="2026-09-16",
        cutoff_at="2026-09-16T14:00:00+08:00",
        generated_at="2026-09-16T14:05:00+08:00",
        sources=[{"id": "official", "status": "success", "item_count": 1, "error": None}],
        events=[{
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
        }],
    )


class DeliveryTest(unittest.TestCase):
    def test_ledger_blocks_sent_and_unknown_but_allows_failed_retry(self):
        edition = document()
        key = DeliveryKey(
            edition["edition_id"], edition["content_hash"], "group", "project-group"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "delivery.json"
            ledger = DeliveryLedger(path)
            self.assertTrue(ledger.may_attempt(key))
            ledger.record(key, "sent")
            self.assertFalse(ledger.may_attempt(key))
            with self.assertRaisesRegex(EditionError, "cannot be downgraded"):
                ledger.record(key, "unknown", detail="late timeout")
            failed_key = DeliveryKey(
                edition["edition_id"], edition["content_hash"], "email", "daily-list-v1"
            )
            ledger.record(failed_key, "unknown", detail="timeout after submit")
            self.assertFalse(ledger.may_attempt(failed_key))
            ledger.record(failed_key, "failed", detail="query confirmed failure")
            self.assertTrue(ledger.may_attempt(failed_key))
            saved = json.loads(path.read_text())

        self.assertNotIn("recipient", json.dumps(saved).replace("recipient_scope", ""))
        self.assertEqual(saved["records"][key.token]["status"], "sent")

    def test_email_and_group_use_independent_keys(self):
        edition = document()
        group = DeliveryKey(
            edition["edition_id"], edition["content_hash"], "group", "project-group"
        )
        email = DeliveryKey(
            edition["edition_id"], edition["content_hash"], "email", "daily-list-v1"
        )
        self.assertNotEqual(group.token, email.token)

    def test_interleaved_channel_writes_preserve_sent_record(self):
        edition = document()
        group = DeliveryKey(
            edition["edition_id"], edition["content_hash"], "group", "project-group"
        )
        email = DeliveryKey(
            edition["edition_id"], edition["content_hash"], "email", "daily-list-v1"
        )
        group_read = threading.Event()
        release_group = threading.Event()
        email_read = threading.Event()
        failures: list[BaseException] = []

        class CoordinatedLedger(DeliveryLedger):
            def _read(self) -> dict:
                payload = super()._read()
                if threading.current_thread().name == "group-writer":
                    group_read.set()
                    if not release_group.wait(2):
                        raise RuntimeError("test did not release group writer")
                elif threading.current_thread().name == "email-writer":
                    email_read.set()
                return payload

        def write(ledger: DeliveryLedger, key: DeliveryKey, status: str) -> None:
            try:
                ledger.record(key, status)  # type: ignore[arg-type]
            except BaseException as exc:
                failures.append(exc)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "delivery.json"
            group_thread = threading.Thread(
                target=write,
                name="group-writer",
                args=(CoordinatedLedger(path), group, "sent"),
            )
            email_thread = threading.Thread(
                target=write,
                name="email-writer",
                args=(CoordinatedLedger(path), email, "failed"),
            )
            group_thread.start()
            self.assertTrue(group_read.wait(1))
            email_thread.start()
            self.assertFalse(email_read.wait(0.1))
            release_group.set()
            group_thread.join(2)
            email_thread.join(2)

            ledger = DeliveryLedger(path)
            self.assertEqual(failures, [])
            self.assertEqual(ledger.status(group), "sent")
            self.assertEqual(ledger.status(email), "failed")
            self.assertFalse(ledger.may_attempt(group))

    def test_local_dogfood_is_viewable_and_does_not_claim_delivery(self):
        html = render_dogfood_page(
            document(), "https://example.com/editions/2026-09-16/"
        )
        self.assertIn("本地 dogfood", html)
        self.assertIn("尚未部署或发送", html)
        self.assertIn("查看网页版", html)
        self.assertIn("查看邮件正文", html)
        self.assertIn("【AI 日报｜2026-09-16】", html)
        self.assertIn("官方发布了一项更新。", html)
        self.assertNotIn("另有", html)
        self.assertNotIn("详见网页版", html)
        self.assertNotIn("OPENAI_API_KEY", html)
        self.assertNotIn("recipient_scope", html)

    def test_email_payload_is_full_email_html_not_a_summary_card(self):
        edition = document()
        public_url = "https://example.com/editions/2026-09-16/"
        parts = build_email_send_parts(edition, public_url)
        group = render_group_message(edition, public_url)

        self.assertEqual(parts["htmlBody"], render_email_html(edition, public_url))
        self.assertEqual(parts["body"], render_email_text(edition, public_url))
        self.assertIn("一条已核验新闻", parts["htmlBody"])
        self.assertIn("官方发布了一项更新。", parts["htmlBody"])
        self.assertIn("官方发布了一项更新。", parts["body"])
        self.assertIn("官方发布了一项更新。", group)
        self.assertNotIn("另有 1 条，详见网页版。", group)
        card = (
            "<html><body><p>一条已核验新闻</p>"
            f'<a href="{public_url}">查看网页版</a></body></html>'
        )
        with self.assertRaisesRegex(EditionError, "complete email.html"):
            require_full_email_html(card, edition, public_url)


if __name__ == "__main__":
    unittest.main()
