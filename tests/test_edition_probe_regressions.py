import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from candidate_policy import assess_new_candidate, review_changed_edition
from delivery.state import DeliveryKey, DeliveryLedger
from edition import EditionError
from execution_binding import BindingError, RunLog
from revision import RevisionWorkspace, require_head_agreement, revision_log_extends
from tests.test_edition_candidate_policy import _edition, _event, _load, _source, _support
from tests.test_edition_revision import HEAD_A, HEAD_B, _contract, _write_workspace

ROOT = Path(__file__).resolve().parents[1]


def _apply(root, contract, current_head=HEAD_A, ledger=None, observed_head=HEAD_A):
    if ledger is None:
        ledger = DeliveryLedger(root / "test-ledger.json")
    return RevisionWorkspace(root).apply(
        contract,
        current_head=current_head,
        ledger=ledger,
        observed_head=observed_head,
    )


def _ids(report):
    return report["retained"], [(item["id"], item["reasons"]) for item in report["rejected"]]


class ProbeRegressionTest(unittest.TestCase):
    def test_probe_cases_that_must_not_pass(self):
        pr65 = _load("pr65-2026-11-07-edition.json")
        pr66 = _load("pr66-2026-11-08-edition.json")
        self.assertEqual(_ids(assess_new_candidate(pr65))[0], [])
        self.assertEqual(_ids(assess_new_candidate(pr66))[0], [])
        secondhand = _edition(
            [
                _event(
                    "s",
                    [
                        _source(
                            "https://example.com/retell",
                            support=_support("retelling", "read", "Later article described it."),
                        )
                    ],
                    placement="lead",
                )
            ]
        )
        self.assertEqual(assess_new_candidate(secondhand)["rejected"][0]["reasons"], ["no_read_original"])
        for name in ("pr65-2026-11-07-edition.json", "pr66-2026-11-08-edition.json"):
            document = _load(name)
            for event in document["events"]:
                for source in event["sources"]:
                    source["support"] = {"relationship": "original", "reading": "read", "excerpt": "x"}
            retained, rejected = _ids(assess_new_candidate(document))
            self.assertEqual(retained, [])
            self.assertTrue(all("excerpt_too_short" in reasons for _, reasons in rejected))

        unrelated = _edition(
            [
                _event(
                    "b",
                    [
                        _source(
                            "https://lab.example/r",
                            support=_support("original", "read", "Cookie policy. All rights reserved."),
                        )
                    ],
                    lede="公司估值达 3000 亿美元。",
                    placement="lead",
                )
            ]
        )
        self.assertIn("excerpt_unrelated", assess_new_candidate(unrelated)["rejected"][0]["reasons"])
        omitted = _edition(
            [
                _event(
                    "d",
                    [
                        _source(
                            "https://lab.example/r",
                            support=_support(excerpt="实测每秒 83.0 token 的页面摘录。"),
                        )
                    ],
                    lede="实测每秒 83.0 token。",
                    placement="lead",
                )
            ]
        )
        self.assertIn(
            "numeric_claim_without_kind",
            assess_new_candidate(omitted)["rejected"][0]["reasons"],
        )
        negated = _edition(
            [
                _event(
                    "e",
                    [
                        _source(
                            "https://lab.example/r",
                            support=_support(excerpt="每秒 83.0 token 的页面摘录。"),
                        )
                    ],
                    lede="每秒 83.0 token，不是预测，是实测。",
                    claims=[{"kind": "predicted", "value": "83.0"}],
                    placement="lead",
                )
            ]
        )
        self.assertIn("prediction_not_visible", assess_new_candidate(negated)["rejected"][0]["reasons"])
        measured = _edition(
            [
                _event(
                    "c",
                    [
                        _source(
                            "https://lab.example/r",
                            support=_support(excerpt="预计每秒 83.0 token。"),
                        )
                    ],
                    lede="预计每秒 83.0 token。",
                    claims=[{"kind": "measured", "value": "83.0"}],
                    placement="lead",
                )
            ]
        )
        self.assertIn(
            "measured_describes_prediction",
            assess_new_candidate(measured)["rejected"][0]["reasons"],
        )

    def test_pr65_real_support_rejects_only_the_funding_round(self):
        document = _load("pr65-2026-11-07-edition.json")
        for event in document["events"]:
            if event["id"] == "openai-funding-round":
                for source in event["sources"]:
                    source["support"] = {
                        "relationship": "retelling",
                        "reading": "read",
                        "excerpt": event["lede"],
                    }
                continue
            import re
            from candidate_policy import _claim_text, _unit_numbers

            text = _claim_text(event)
            numbers = sorted(_unit_numbers(text), key=len, reverse=True)
            span = next(iter(re.findall(r"[\u4e00-\u9fff]{6,}", event.get("lede") or "")), "来源页核对记录")
            if numbers:
                excerpt = "来源页记录的数字是 " + "、".join(numbers) + " 。补充核对。"
                event["claims"] = [{"kind": "measured", "value": number} for number in numbers]
            else:
                excerpt = f"核对记录写到{span[:6]}，这句话不是正文整段复制。"
            named = next((number for number in numbers if len(number) >= 4), span[:6])
            event["sources"][0]["support"] = {
                "relationship": "original",
                "reading": "read",
                "excerpt": excerpt,
            }
            event["editorial_verifications"] = [
                {
                    "edition_id": document["edition_id"],
                    "attempt": document["attempt"],
                    "source_url": event["sources"][0]["url"],
                    "excerpt": excerpt,
                    "claim": named,
                    "verdict": "supports",
                }
            ]
        report = assess_new_candidate(document)
        self.assertEqual(
            [item["id"] for item in report["rejected"]],
            ["openai-funding-round"],
        )
        self.assertEqual(
            report["retained"],
            [
                "biobasics-bench",
                "anthropic-agent-memory",
                "muse-spaces",
                "hacktron-partnership",
            ],
        )

    def test_published_edition_change_is_frozen_and_sent_revision_is_refused(self):
        text = (ROOT / "editions" / "2026-11-08" / "edition.json").read_text(encoding="utf-8")
        mutated = json.loads(text)
        for event in mutated["events"]:
            event["lede"] = event["lede"] + "（改写）"
        with self.assertRaisesRegex(EditionError, "published edition is frozen"):
            review_changed_edition(text, json.dumps(mutated, ensure_ascii=False))
        self.assertEqual(review_changed_edition(text, text), "historical")

        document = _edition(
            [
                _event(
                    "keep",
                    [_source("https://lab.example/r", support=_support(excerpt="读者看到的导语与结果。"))],
                    placement="lead",
                ),
                _event(
                    "drop",
                    [
                        _source(
                            "https://n.example/r",
                            support=_support("retelling", "read", "Later article described the report."),
                        )
                    ],
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "ws"
            workspace.mkdir()
            _write_workspace(workspace, document)
            ledger = DeliveryLedger(root / "ledger.json")
            key = DeliveryKey(document["edition_id"], document["content_hash"], "email", "daily-list-v1")
            ledger.record(key, "sent")
            with self.assertRaisesRegex(EditionError, "frozen after delivery"):
                _apply(
                    workspace,
                    _contract(document, approved_removals=["drop"]),
                    ledger=ledger,
                )

    def test_revision_log_blocks_reuse_and_rewrites(self):
        document = _edition(
            [
                _event(
                    "drop",
                    [
                        _source(
                            "https://n.example/r",
                            support=_support("retelling", "read", "Later article described the report."),
                        )
                    ],
                    placement="lead",
                )
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_workspace(root, document)
            _apply(root, 
                _contract(document, approved_removals=["drop"]),
                current_head=HEAD_A,
            )
            log = (root / "revisions.json").read_text(encoding="utf-8")
            (root / "revision-state.json").unlink()
            _write_workspace(root, document)
            with self.assertRaisesRegex(EditionError, "already used"):
                _apply(root, 
                    _contract(document, approved_removals=["drop"]),
                    current_head=HEAD_A,
                )
        with self.assertRaisesRegex(EditionError, "rewritten"):
            revision_log_extends(log, log.replace("applied", "blocked"))

    def test_old_editorial_head_and_html_oserror_do_not_publish(self):
        document = _edition(
            [
                _event(
                    "kept",
                    [_source("https://example.com/original", support=_support())],
                    placement="lead",
                )
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_workspace(root, document)
            before = (root / "edition.json").read_bytes()
            with self.assertRaisesRegex(EditionError, "not bound to this head"):
                _apply(root, 
                    _contract(
                        document,
                        base_head=HEAD_B,
                        approved_removals=["kept"],
                        editorial_rejections=[
                            {
                                "id": "kept",
                                "reasons": ["editor rejected on the old head"],
                                "edition_id": document["edition_id"],
                                "attempt": document["attempt"],
                                "base_head": HEAD_A,
                            }
                        ],
                    ),
                    current_head=HEAD_B,
                    observed_head=HEAD_B,
                )
            self.assertEqual((root / "edition.json").read_bytes(), before)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_workspace(root, document)
            before = (root / "edition.json").read_bytes()

            def boom(self, path, html):
                raise OSError("disk full")

            with patch.object(RevisionWorkspace, "_write_html_text", boom):
                with self.assertRaises(OSError):
                    _apply(root, 
                        _contract(
                            document,
                            approved_wording=[
                                {"event_id": "kept", "field": "lede", "value": "读者看到的导语。已澄清。"}
                            ],
                            editorial_rejections=[
                                {
                                    "id": "kept",
                                    "reasons": ["clarify the lede"],
                                    "edition_id": document["edition_id"],
                                    "attempt": document["attempt"],
                                    "base_head": HEAD_A,
                                }
                            ],
                        ),
                        current_head=HEAD_A,
                    )
            self.assertEqual((root / "edition.json").read_bytes(), before)
            self.assertFalse((root / "index.html").exists())

    def test_attempted_revision_can_resume_when_the_file_is_unchanged(self):
        document = _edition(
            [
                _event(
                    "drop",
                    [
                        _source(
                            "https://n.example/r",
                            support=_support("retelling", "read", "Later article described the report."),
                        )
                    ],
                    placement="lead",
                )
            ]
        )
        contract = _contract(document, approved_removals=["drop"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_workspace(root, document)
            state = {
                "version": 1,
                "completions": {
                    f"{document['edition_id']}|1|1": {
                        "contract": contract,
                        "status": "attempted",
                        "reason": None,
                        "result": None,
                    }
                },
            }
            (root / "revision-state.json").write_text(json.dumps(state), encoding="utf-8")
            (root / "revisions.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "records": [
                            {
                                "edition_id": document["edition_id"],
                                "attempt": 1,
                                "revision": 1,
                                "status": "attempted",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            result = _apply(root, contract)
            self.assertEqual(result["status"], "failed")
            self.assertEqual((root / "edition.json").read_text(encoding="utf-8").count('"failed"') > 0, True)

    def test_observed_head_mismatch_is_rejected(self):
        with self.assertRaisesRegex(EditionError, "observed head"):
            require_head_agreement(HEAD_A, HEAD_B)


class RunBindingTest(unittest.TestCase):
    def _evidence(self, run_id="run-1", **overrides):
        evidence = {
            "run_id": run_id,
            "terminal_state": "finished",
            "source": "wake_payload",
            "head": HEAD_A,
            "content_hash": "sha256:" + "ab" * 32,
        }
        evidence.update(overrides)
        return evidence

    def test_dispatch_stores_only_its_own_run_id(self):
        log = RunLog()
        stored = log.dispatch(
            {"run_id": "run-one", "agent_id": "bc-other", "edition_id": "2026-11-12", "expected_head": HEAD_A}
        )
        self.assertEqual(stored["run_id"], "run-one")
        with self.assertRaises(BindingError):
            log.dispatch({"run_id": "bc-5968f500-228c-5e05-938e-0233cf57c6aa"})
        with self.assertRaises(BindingError):
            log.dispatch({"run_id": "exec-unknown"})
        with self.assertRaises(BindingError):
            log.dispatch({"latest_run_id": "run-two"})

    def test_completion_binds_the_same_run_and_rejects_the_rest(self):
        log = RunLog()
        log.dispatch({"run_id": "run-older", "edition_id": "2026-11-12", "expected_head": HEAD_A})
        log.dispatch({"run_id": "run-current", "edition_id": "2026-11-12", "expected_head": HEAD_A})
        blocked = log.complete({"run_id": "run-current"})
        self.assertEqual(blocked["status"], "blocked")
        self.assertIn("source", blocked["missing"])
        self.assertIsNone(blocked["execution_id"])
        rejected = log.complete(self._evidence("run-current", source="agent_status"))
        self.assertEqual(rejected["reason"], "completion source is not a per-run receipt")
        late = log.complete(self._evidence("run-older"))
        self.assertEqual(late["reason"], "late wake from an older run")
        accepted = log.complete(self._evidence("run-current", source="run_query"))
        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(accepted["head"], HEAD_A)
        self.assertEqual(accepted["execution_id"], "run-current")
        duplicate = log.complete(self._evidence("run-current", source="run_query"))
        self.assertEqual(duplicate["reason"], "duplicate wake")
        foreign = log.complete(self._evidence("run-foreign"))
        self.assertEqual(foreign["reason"], "completion run was not dispatched")
        prefixed = log.complete(self._evidence("bc-agent"))
        self.assertEqual(prefixed["reason"], "agent id is not a run id")
