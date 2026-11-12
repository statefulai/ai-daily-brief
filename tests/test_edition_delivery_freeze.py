import json
import tempfile
import threading
import unittest
from pathlib import Path

from delivery.state import DeliveryKey, DeliveryLedger
from edition import EditionError, validate_edition
from tests.test_edition_candidate_policy import _edition, _event, _source, _support


def _key(edition, channel, digest=None):
    return DeliveryKey(
        edition["edition_id"],
        digest or edition["content_hash"],
        channel,
        "daily-list-v1" if channel == "email" else "project-group",
    )


class DeliveryFreezeTest(unittest.TestCase):
    def _document(self):
        return _edition(
            [
                _event(
                    "kept",
                    [_source("https://example.com/original", support=_support())],
                    placement="lead",
                )
            ]
        )

    def test_sent_hash_freezes_the_edition_and_a_new_hash_cannot_send(self):
        edition = self._document()
        revised = json.loads(json.dumps(edition))
        revised["events"][0]["lede"] = "改过的导语。"
        revised.pop("content_hash", None)
        revised = validate_edition(revised)
        self.assertNotEqual(edition["content_hash"], revised["content_hash"])
        group = _key(edition, "group")
        email = _key(edition, "email")
        new_group = _key(revised, "group")
        with tempfile.TemporaryDirectory() as directory:
            ledger = DeliveryLedger(Path(directory) / "delivery.json")
            self.assertTrue(ledger.may_attempt(group))
            ledger.record(group, "sent")
            self.assertFalse(ledger.may_attempt(group))
            self.assertTrue(ledger.may_attempt(email))
            self.assertFalse(ledger.may_attempt(new_group))
            with self.assertRaisesRegex(EditionError, "frozen"):
                ledger.record(new_group, "sent")
            ledger.record(email, "unknown", detail="timeout after submit")
            self.assertFalse(ledger.may_attempt(email))
            self.assertFalse(ledger.may_attempt(new_group))

    def test_unknown_without_a_sent_receipt_blocks_a_different_hash(self):
        edition = self._document()
        revised = json.loads(json.dumps(edition))
        revised["events"][0]["lede"] = "另一版导语。"
        revised.pop("content_hash", None)
        revised = validate_edition(revised)
        email = _key(edition, "email")
        new_group = _key(revised, "group")
        with tempfile.TemporaryDirectory() as directory:
            ledger = DeliveryLedger(Path(directory) / "delivery.json")
            ledger.record(email, "unknown", detail="timeout after submit")
            self.assertFalse(ledger.may_attempt(email))
            self.assertFalse(ledger.may_attempt(new_group))

    def test_begin_attempt_reserves_under_the_lock(self):
        edition = self._document()
        group = _key(edition, "group")
        entered = threading.Event()
        release = threading.Event()
        failures: list[BaseException] = []

        class HoldingLedger(DeliveryLedger):
            def _read(self):
                payload = super()._read()
                if threading.current_thread().name == "first":
                    entered.set()
                    if not release.wait(2):
                        raise RuntimeError("test did not release the first writer")
                return payload

        def reserve(ledger):
            try:
                ledger.begin_attempt(group)
            except BaseException as exc:
                failures.append(exc)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "delivery.json"
            first = threading.Thread(target=reserve, name="first", args=(HoldingLedger(path),))
            second = threading.Thread(target=reserve, name="second", args=(HoldingLedger(path),))
            first.start()
            self.assertTrue(entered.wait(1))
            second.start()
            release.set()
            first.join(2)
            second.join(2)
            ledger = DeliveryLedger(path)
            self.assertEqual(ledger.status(group), "unknown")
            self.assertFalse(ledger.may_attempt(group))
            self.assertTrue(any(isinstance(item, EditionError) for item in failures))
            self.assertIn("not open", str(failures[0]))

    def test_direct_record_cannot_freeze_a_new_hash_while_another_is_unknown(self):
        edition = self._document()
        revised = json.loads(json.dumps(edition))
        revised["events"][0]["lede"] = "另一版导语。"
        revised.pop("content_hash", None)
        revised = validate_edition(revised)
        unknown = _key(edition, "email")
        sent = _key(revised, "group")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "delivery.json"
            ledger = DeliveryLedger(path)
            ledger.record(unknown, "unknown", detail="timeout after submit")
            with self.assertRaisesRegex(EditionError, "unknown for another content hash"):
                ledger.record(sent, "sent")
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn(revised["content_hash"], json.dumps(saved.get("frozen_editions", {})))
            self.assertEqual(ledger.status(unknown), "unknown")
            self.assertTrue(ledger.may_attempt(_key(edition, "group")))

    def test_record_sent_waits_for_the_unknown_writer(self):
        edition = self._document()
        revised = json.loads(json.dumps(edition))
        revised["events"][0]["lede"] = "竞争导语。"
        revised.pop("content_hash", None)
        revised = validate_edition(revised)
        unknown = _key(edition, "email")
        sent = _key(revised, "group")
        entered = threading.Event()
        release = threading.Event()
        failures: list[BaseException] = []

        class HoldingLedger(DeliveryLedger):
            def _read(self):
                payload = super()._read()
                if threading.current_thread().name == "unknown-writer":
                    entered.set()
                    if not release.wait(2):
                        raise RuntimeError("test did not release the unknown writer")
                return payload

        def write_unknown(ledger):
            try:
                ledger.record(unknown, "unknown", detail="timeout after submit")
            except BaseException as exc:
                failures.append(exc)

        def write_sent(ledger):
            try:
                ledger.record(sent, "sent")
            except BaseException as exc:
                failures.append(exc)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "delivery.json"
            first = threading.Thread(
                target=write_unknown, name="unknown-writer", args=(HoldingLedger(path),)
            )
            second = threading.Thread(
                target=write_sent, name="sent-writer", args=(HoldingLedger(path),)
            )
            first.start()
            self.assertTrue(entered.wait(1))
            second.start()
            release.set()
            first.join(2)
            second.join(2)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(any(isinstance(item, EditionError) for item in failures))
            self.assertNotIn(revised["content_hash"], json.dumps(saved.get("frozen_editions", {})))
            self.assertEqual(DeliveryLedger(path).status(unknown), "unknown")
