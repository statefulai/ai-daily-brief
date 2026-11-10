import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from candidate_policy import MIN_EXCERPT_CHARS, assess_new_candidate
from delivery.state import DeliveryKey, DeliveryLedger
from edition import EditionError, validate_edition
from execution_binding import BindingError, RunLog
from outputs import render_web_edition
from revision import RevisionWorkspace, revision_log_extends
from scripts.edition_ci import (
    checkout_head_matches_event,
    preflight_candidate,
    validate_changed_paths,
)
from tests.test_edition_candidate_policy import _edition, _event, _load, _source, _support
from tests.test_edition_revision import HEAD_A, _contract, _write_workspace

ROOT = Path(__file__).resolve().parents[1]
URL = "https://lab.example/r"


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _rev(repo):
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _assess(event):
    report = assess_new_candidate(_edition([event]))
    return report["retained"], report["rejected"]


class ReviewRegressionTest(unittest.TestCase):
    def test_semantic_probe_cases_reject_except_the_open_measured_gap(self):
        unrelated_claim = _event(
            "s1b",
            [_source(URL, support=_support("original", "read", "The company valuation reached 3000 at close."))],
            lede="公司估值达 3000 亿美元。",
            claims=[{"kind": "attributed", "value": "无关"}],
            placement="lead",
        )
        retained, rejected = _assess(unrelated_claim)
        self.assertEqual(retained, [])
        self.assertIn("numeric_claim_uncovered", rejected[0]["reasons"])
        self.assertIn("claim_value_not_in_text", rejected[0]["reasons"])

        missing_value = _event(
            "s1c",
            [_source(URL, support=_support("original", "read", "Throughput reached 83.0 tokens per second."))],
            lede="每秒 83.0 token。",
            claims=[{"kind": "measured", "value": "999"}],
            placement="lead",
        )
        retained, rejected = _assess(missing_value)
        self.assertEqual(retained, [])
        self.assertIn("claim_value_not_in_text", rejected[0]["reasons"])
        self.assertIn("numeric_claim_uncovered", rejected[0]["reasons"])

        for lede in (
            "估值达 $300B，融资完成。",
            "估值达 300 billion 美金。",
            "延迟降到 20ms，速度提升明显。",
            "吞吐提升 3x，速度提升明显。",
            "基准得分 91.2 分，速度提升明显。",
            "显存 141GB，速度提升明显。",
        ):
            retained, rejected = _assess(
                _event("s2", [_source(URL, support=_support(excerpt="Benchmark update for this release."))], lede=lede, placement="lead")
            )
            self.assertEqual(retained, [])
            self.assertIn("numeric_claim_without_kind", rejected[0]["reasons"])

        for lede in (
            "每秒 83.0 token，并不是预测，是实测。",
            "每秒 83.0 token，非预测数据。",
            "每秒 83.0 token，不属于预测。",
            "每秒 83.0 token，不是 预测。",
            "每秒 83.0 token (not a forecast)。",
            "每秒 83.0 token, not projected。",
            "每秒 83.0 token，并非是预计。",
        ):
            retained, rejected = _assess(
                _event(
                    "s3",
                    [_source(URL, support=_support(excerpt="The lab measured 83.0 tokens per second."))],
                    lede=lede,
                    claims=[{"kind": "predicted", "value": "83.0"}],
                    placement="lead",
                )
            )
            self.assertEqual(retained, [])
            self.assertIn("prediction_not_visible", rejected[0]["reasons"])

        retained, _rejected = _assess(
            _event(
                "s4",
                [_source(URL, support=_support(excerpt="Projected throughput is 83.0 tokens per second."))],
                lede="实测每秒 83.0 token。",
                claims=[{"kind": "measured", "value": "83.0"}],
                placement="lead",
            )
        )
        self.assertEqual(retained, ["s4"])

        for event_id, excerpt, lede in (
            ("s5", "据外媒报道，该公司宣布完成融资。", "该公司宣布新模型上线。"),
            ("s6", "Cookie policy © 2026. All rights reserved.", "2026 年公司估值达 3000 亿美元。"),
            ("s6b", "Cookie policy for this website and model pages.", "新 model 估值达 3000 亿美元。"),
        ):
            claims = [{"kind": "attributed", "value": "3000"}] if "3000" in lede else None
            retained, rejected = _assess(
                _event(
                    event_id,
                    [_source(URL, support=_support("original", "read", excerpt))],
                    lede=lede,
                    claims=claims,
                    placement="lead",
                )
            )
            self.assertEqual(retained, [])
            self.assertTrue(rejected)

        for claim in ("。", "焦点"):
            retained, rejected = _assess(
                _event(
                    "s7",
                    [_source(URL, support=_support(excerpt="The lab reported 91.2 on the benchmark."))],
                    lede="实验室报告基准得分 91.2。",
                    claims=[{"kind": "measured", "value": "91.2"}],
                    placement="lead",
                )
            )
            event = retained  # silence unused if the helper attached the lede
            del event
            document = _edition(
                [
                    _event(
                        "s7",
                        [_source(URL, support=_support(excerpt="The lab reported 91.2 on the benchmark."))],
                        lede="实验室报告基准得分 91.2。",
                        claims=[{"kind": "measured", "value": "91.2"}],
                        placement="lead",
                    )
                ]
            )
            document["events"][0]["editorial_verifications"][0]["claim"] = claim
            report = assess_new_candidate(document)
            self.assertEqual(report["retained"], [])
            self.assertIn("missing_editorial_verification", report["rejected"][0]["reasons"])

        short = "91.2 分数结果a"
        self.assertLess(len(short), MIN_EXCERPT_CHARS)
        self.assertEqual(MIN_EXCERPT_CHARS, 12)
        retained, rejected = _assess(
            _event(
                "s8",
                [_source(URL, support=_support("original", "read", short))],
                lede="得分 91.2。",
                claims=[{"kind": "measured", "value": "91.2"}],
                placement="lead",
            )
        )
        self.assertEqual(retained, [])
        self.assertIn("excerpt_too_short", rejected[0]["reasons"])

        copied = _load("pr65-2026-11-07-edition.json")
        for event in copied["events"]:
            excerpt = event["lede"]
            for source in event["sources"]:
                source["support"] = {"relationship": "original", "reading": "read", "excerpt": excerpt}
            event["claims"] = [{"kind": "attributed", "value": "x"}]
            event["editorial_verifications"] = [
                {
                    "edition_id": copied["edition_id"],
                    "attempt": copied["attempt"],
                    "source_url": source["url"],
                    "excerpt": excerpt,
                    "claim": excerpt,
                    "verdict": "supports",
                }
                for source in event["sources"]
            ]
        report = assess_new_candidate(copied)
        self.assertEqual(report["retained"], [])
        self.assertTrue(all("excerpt_copies_text" in item["reasons"] for item in report["rejected"]))

    def test_scope_accepts_revisions_json_only_with_the_edition(self):
        new_edition = [
            "editions/2026-12-01/edition.json",
            "editions/2026-12-01/index.html",
            "editions/2026-12-01/revisions.json",
        ]
        validate_changed_paths(new_edition, deleted_paths=[])
        with self.assertRaisesRegex(EditionError, "only with edition.json and index.html"):
            validate_changed_paths(["editions/2026-11-08/revisions.json"], deleted_paths=[])
        with self.assertRaises(EditionError) as caught:
            revision_log_extends(None, json.dumps({"records": [{"revision": None}]}))
        self.assertIsInstance(caught.exception, EditionError)
        with self.assertRaises(EditionError):
            revision_log_extends(
                None,
                json.dumps({"records": [{"revision": 7, "status": "applied", "edition_id": "1999-01-01"}]}),
            )

    def test_scope_command_on_a_temp_repo_accepts_and_rejects_revision_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            _git(repo, "init")
            (repo / "README").write_text("base\n", encoding="utf-8")
            _git(repo, "add", "README")
            _git(repo, "commit", "-m", "base")
            base = _rev(repo)
            document = _edition(
                [_event("kept", [_source(URL, support=_support())], placement="lead")],
                edition_id="2026-12-01",
            )
            edition_dir = repo / "editions" / "2026-12-01"
            edition_dir.mkdir(parents=True)
            (edition_dir / "edition.json").write_text(
                json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            (edition_dir / "index.html").write_text(render_web_edition(document), encoding="utf-8")
            record = {
                "version": 1,
                "records": [
                    {
                        "edition_id": "2026-12-01",
                        "attempt": 1,
                        "revision": 1,
                        "base_head": HEAD_A,
                        "status": "applied",
                        "reason": None,
                    }
                ],
            }
            (edition_dir / "revisions.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            _git(repo, "add", "editions")
            _git(repo, "commit", "-m", "edition")
            completed = subprocess.run(
                [
                    "python3",
                    str(ROOT / "scripts" / "edition_ci.py"),
                    "scope",
                    "--base",
                    base,
                    "--head",
                    "HEAD",
                    "--repo",
                    str(repo),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("validated change scope", completed.stdout)
            preflight_candidate(edition_dir / "edition.json", base, repo)

            published = repo / "editions" / "2026-11-08"
            published.mkdir()
            for name in ("edition.json", "index.html"):
                (published / name).write_bytes((ROOT / "editions" / "2026-11-08" / name).read_bytes())
            _git(repo, "add", "editions/2026-11-08")
            _git(repo, "commit", "-m", "published")
            published_base = _rev(repo)
            (published / "revisions.json").write_text(
                json.dumps(record, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            _git(repo, "add", "editions/2026-11-08/revisions.json")
            _git(repo, "commit", "-m", "log only")
            rejected = subprocess.run(
                [
                    "python3",
                    str(ROOT / "scripts" / "edition_ci.py"),
                    "scope",
                    "--base",
                    published_base,
                    "--head",
                    "HEAD",
                    "--repo",
                    str(repo),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("revisions.json", rejected.stderr + rejected.stdout)

    def test_ledger_and_observed_head_are_required_and_reuse_is_against_base(self):
        document = _edition(
            [
                _event("keep", [_source(URL, support=_support(excerpt="The lab reported 91.2 on the benchmark."))], claims=[{"kind": "measured", "value": "91.2"}], placement="lead"),
                _event(
                    "drop",
                    [_source("https://n.example/r", support=_support("retelling", "read", "后来的转述文章内容。"))],
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_workspace(root, document)
            ledger = DeliveryLedger(root / "ledger.json")
            key = DeliveryKey(document["edition_id"], document["content_hash"], "email", "daily-list-v1")
            ledger.record(key, "sent")
            contract = _contract(document, approved_removals=["drop"])
            with self.assertRaisesRegex(EditionError, "frozen after delivery"):
                RevisionWorkspace(root).apply(
                    contract,
                    current_head=HEAD_A,
                    ledger=ledger,
                    observed_head=HEAD_A,
                )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_workspace(root, document)
            with self.assertRaisesRegex(EditionError, "delivery ledger is required"):
                RevisionWorkspace(root).apply(contract, current_head=HEAD_A)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_workspace(root, document)
            fresh = DeliveryLedger(root / "other-ledger.json")
            with self.assertRaisesRegex(EditionError, "observed head is required"):
                RevisionWorkspace(root).apply(
                    contract,
                    current_head=HEAD_A,
                    ledger=fresh,
                    observed_head=None,
                )

        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            _git(repo, "init")
            edition_dir = repo / "editions" / document["edition_id"]
            edition_dir.mkdir(parents=True)
            (edition_dir / "edition.json").write_text(json.dumps(document, ensure_ascii=False) + "\n", encoding="utf-8")
            (edition_dir / "index.html").write_text(render_web_edition(document), encoding="utf-8")
            first = {
                "version": 1,
                "records": [
                    {
                        "edition_id": document["edition_id"],
                        "attempt": 1,
                        "revision": 1,
                        "base_head": HEAD_A,
                        "status": "applied",
                        "reason": None,
                    }
                ],
            }
            log_path = edition_dir / "revisions.json"
            log_path.write_text(json.dumps(first) + "\n", encoding="utf-8")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "log")
            base_text = log_path.read_text(encoding="utf-8")
            rewritten = json.loads(base_text)
            rewritten["records"][0]["reason"] = "reused"
            log_path.write_text(json.dumps(rewritten) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(EditionError, "rewritten"):
                revision_log_extends(base_text, log_path.read_text(encoding="utf-8"), edition_id=document["edition_id"])

    def test_completion_evidence_format_and_event_head(self):
        log = RunLog()
        log.dispatch({"run_id": "run-AAA", "agent_id": "bc-1", "expected_head": HEAD_A})
        rejected = log.complete(
            {
                "run_id": "run-AAA",
                "terminal_state": "finished",
                "source": "wake_payload",
                "head": "whatever",
                "content_hash": "anything",
            }
        )
        self.assertEqual(rejected["status"], "rejected")
        self.assertIn("40 hexadecimal", rejected["reason"])
        accepted = log.complete(
            {
                "run_id": "run-AAA",
                "terminal_state": "finished",
                "source": "run_query",
                "head": HEAD_A,
                "content_hash": "sha256:" + "0" * 64,
            }
        )
        self.assertEqual(accepted["status"], "accepted")
        self.assertTrue(accepted["source_is_self_reported"])
        with self.assertRaisesRegex(BindingError, "empty suffix"):
            RunLog().dispatch({"run_id": "run-"})
        mismatched = RunLog()
        mismatched.dispatch({"run_id": "run-BBB", "expected_head": HEAD_A})
        wrong_head = mismatched.complete(
            {
                "run_id": "run-BBB",
                "terminal_state": "finished",
                "source": "wake_payload",
                "head": "b" * 40,
                "content_hash": "sha256:" + "1" * 64,
            }
        )
        self.assertEqual(wrong_head["reason"], "head does not match the dispatch")

        interleaved = RunLog()
        interleaved.dispatch({"run_id": "run-ED1", "edition_id": "2026-11-12"})
        interleaved.dispatch({"run_id": "run-ED2", "edition_id": "2026-11-13"})
        first = interleaved.complete(
            {
                "run_id": "run-ED1",
                "terminal_state": "finished",
                "source": "wake_payload",
                "head": "c" * 40,
                "content_hash": "sha256:" + "2" * 64,
            }
        )
        self.assertEqual(first["status"], "accepted")
        same = RunLog()
        same.dispatch({"run_id": "run-old", "edition_id": "2026-11-12"})
        same.dispatch({"run_id": "run-new", "edition_id": "2026-11-12"})
        late = same.complete(
            {
                "run_id": "run-old",
                "terminal_state": "finished",
                "source": "wake_payload",
                "head": "d" * 40,
                "content_hash": "sha256:" + "3" * 64,
            }
        )
        self.assertEqual(late["reason"], "late wake from an older run")

        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            _git(repo, "init")
            (repo / "README").write_text("head\n", encoding="utf-8")
            _git(repo, "add", "README")
            _git(repo, "commit", "-m", "head")
            head = _rev(repo)
            event = repo / "event.json"
            event.write_text(json.dumps({"pull_request": {"head": {"sha": head}}}), encoding="utf-8")
            self.assertEqual(checkout_head_matches_event(repo, event), head)
            event.write_text(json.dumps({"pull_request": {"head": {"sha": "0" * 40}}}), encoding="utf-8")
            with self.assertRaisesRegex(EditionError, "does not match the pull request head"):
                checkout_head_matches_event(repo, event)


if __name__ == "__main__":
    unittest.main()
