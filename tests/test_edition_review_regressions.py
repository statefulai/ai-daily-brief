import json
import os
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


def _scope_env():
    env = os.environ.copy()
    env.pop("GITHUB_EVENT_PATH", None)
    return env


def _git(repo, *args):
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = "Edition Test"
    env["GIT_AUTHOR_EMAIL"] = "edition-test@example.com"
    env["GIT_COMMITTER_NAME"] = "Edition Test"
    env["GIT_COMMITTER_EMAIL"] = "edition-test@example.com"
    subprocess.run(
        ["git", "-c", "user.name=Edition Test", "-c", "user.email=edition-test@example.com", *args],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


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
                env=_scope_env(),
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
                env=_scope_env(),
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
        log.dispatch(
            {
                "run_id": "run-AAA",
                "agent_id": "bc-1",
                "edition_id": "2026-11-12",
                "expected_head": HEAD_A,
            }
        )
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
        mismatched.dispatch(
            {"run_id": "run-BBB", "edition_id": "2026-11-12", "expected_head": HEAD_A}
        )
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
        interleaved.dispatch(
            {"run_id": "run-ED1", "edition_id": "2026-11-12", "expected_head": "c" * 40}
        )
        interleaved.dispatch(
            {"run_id": "run-ED2", "edition_id": "2026-11-13", "expected_head": "c" * 40}
        )
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
        same.dispatch({"run_id": "run-old", "edition_id": "2026-11-12", "expected_head": "d" * 40})
        same.dispatch({"run_id": "run-new", "edition_id": "2026-11-12", "expected_head": "d" * 40})
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
            event.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(EditionError, "no comparable head"):
                checkout_head_matches_event(repo, event)
            event.write_text(json.dumps({"after": head}), encoding="utf-8")
            self.assertEqual(checkout_head_matches_event(repo, event), head)

    def test_merge_commit_checkout_uses_the_second_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            _git(repo, "init", "-b", "main")
            (repo / "README").write_text("base\n", encoding="utf-8")
            _git(repo, "add", "README")
            _git(repo, "commit", "-m", "base")
            _git(repo, "checkout", "-b", "feature")
            (repo / "README").write_text("feature\n", encoding="utf-8")
            _git(repo, "add", "README")
            _git(repo, "commit", "-m", "feature")
            feature = _rev(repo)
            _git(repo, "checkout", "main")
            _git(repo, "merge", "--no-ff", "-m", "merge", "feature")
            merge = _rev(repo)
            self.assertNotEqual(merge, feature)
            event = repo / "event.json"
            event.write_text(
                json.dumps({"pull_request": {"head": {"sha": feature}}}),
                encoding="utf-8",
            )
            self.assertEqual(checkout_head_matches_event(repo, event), feature)
            event.write_text(
                json.dumps({"pull_request": {"head": {"sha": merge}}}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(EditionError, "second parent"):
                checkout_head_matches_event(repo, event)

    def test_new_numeric_forms_negation_and_translation_bridge(self):
        forms = [
            "公司估值达三千亿美元，面向开发者开放下载。",
            "推理成本下降百分之二十，面向开发者开放下载。",
            "公司估值达３０００亿美元，面向开发者开放下载。",
            "公司估值达 3 千亿美元，面向开发者开放下载。",
            "新模型 70B 参数，面向开发者开放下载。",
            "上下文扩展到 128K，面向开发者开放下载。",
            "订阅价格每月 20 美金，面向开发者开放。",
            "生成视频可达 60fps，面向开发者开放。",
            "生成一张图只需 3 秒，面向开发者开放。",
            "训练时长缩短到 2 小时，面向开发者开放。",
            "市场规模达 1.5 万亿美元，面向开发者开放。",
            "数据集 5TB，面向开发者开放下载。",
            "用户数达 1000 万，面向开发者开放下载。",
        ]
        self.assertEqual(len(forms), 13)
        for lede in forms:
            retained, rejected = _assess(
                _event(
                    "n1",
                    [_source(URL, support=_support(excerpt="Source page says the model is open for download now."))],
                    lede=lede,
                    placement="lead",
                )
            )
            self.assertEqual(retained, [])
            self.assertTrue(
                "numeric_claim_without_kind" in rejected[0]["reasons"]
                or "numeric_claim_uncovered" in rejected[0]["reasons"]
            )
        retained, rejected = _assess(
            _event(
                "n2b",
                [_source(URL, support=_support(excerpt="Revenue was 300 and margin 20 percent this year at the lab."))],
                lede="营收 300 亿美元，利润率 20%。",
                claims=[{"kind": "measured", "value": "营收 300 亿美元，利润率 20%"}],
                placement="lead",
            )
        )
        self.assertEqual(retained, [])
        self.assertIn("claim_value_not_a_span", rejected[0]["reasons"])
        retained, rejected = _assess(
            _event(
                "n3",
                [_source(URL, support=_support(excerpt="The lab reported 83.0 tokens per second on the public benchmark today."))],
                lede="每秒 83.0 token，这个数并不属于厂商官方正式给出的预测。",
                claims=[{"kind": "predicted", "value": "83.0"}],
                placement="lead",
            )
        )
        self.assertEqual(retained, [])
        self.assertIn("prediction_not_visible", rejected[0]["reasons"])
        for event_id, lede in (
            ("n3b", "每秒 83.0 token，非常乐观的预测值。"),
            ("n3c", "公司不久前预计每秒 83.0 token。"),
        ):
            retained, rejected = _assess(
                _event(
                    event_id,
                    [_source(URL, support=_support(excerpt="Projected throughput is 83.0 tokens per second for the lab."))],
                    lede=lede,
                    claims=[{"kind": "predicted", "value": "83.0"}],
                    placement="lead",
                )
            )
            self.assertEqual(rejected, [], rejected)
            self.assertEqual(retained, [event_id])
        retained, rejected = _assess(
            _event(
                "n3d",
                [_source(URL, support=_support(excerpt="Projected throughput is 83.0 tokens per second for the lab."))],
                lede="实测每秒 83.0 token，非常接近预计。",
                claims=[{"kind": "measured", "value": "83.0"}],
                placement="lead",
            )
        )
        self.assertEqual(retained, [])
        self.assertIn("measured_describes_prediction", rejected[0]["reasons"])

        with self.assertRaisesRegex(BindingError, "edition_id"):
            RunLog().dispatch({"run_id": "run-OLD", "expected_head": HEAD_A})
        with self.assertRaisesRegex(BindingError, "expected_head"):
            RunLog().dispatch({"run_id": "run-A", "edition_id": "2026-11-12"})

        bare = [
            _event(
                "n5",
                [_source("https://www.anthropic.com/news/memory", support=_support("original", "read", "Today we're bringing memory to Claude for Team and Enterprise users."))],
                lede="Anthropic 为 Claude 团队版和企业版用户上线记忆功能，可在设置中开启。",
                placement="lead",
            ),
            _event(
                "n5c",
                [_source("https://lab.example/plan", support=_support("original", "read", "The company expects to reach 1 million users by Q2."))],
                lede="公司预计第二季度用户达到 100 万。",
                claims=[{"kind": "predicted", "value": "100 万"}],
                placement="lead",
            ),
            _event(
                "n5d",
                [_source("https://lab.example/r2", support=_support("original", "read", "Our 2026 roadmap introduces agents for enterprise customers and developers."))],
                lede="公司发布 2026 年路线图，面向企业客户推出智能体。",
                placement="lead",
            ),
        ]
        rejected_bare = sum(1 for event in bare if not assess_new_candidate(_edition([event]))["retained"])
        self.assertEqual(rejected_bare, 3)
        bridged = _edition(
            [
                _event(
                    "n5",
                    [_source("https://www.anthropic.com/news/memory", support=_support("original", "read", "Today we're bringing memory to Claude for Team and Enterprise users."))],
                    lede="Anthropic 为 Claude 团队版和企业版用户上线记忆功能，可在设置中开启。",
                    placement="lead",
                )
            ]
        )
        bridged["events"][0]["editorial_verifications"][0]["source_span"] = (
            "Today we're bringing memory to Claude for Team and Enterprise users."
        )
        bridged["events"][0]["editorial_verifications"][0]["translated_claim"] = "团队版和企业版用户上线记忆功能"
        bridged.pop("content_hash", None)
        bridged = validate_edition(bridged)
        self.assertEqual(assess_new_candidate(bridged)["retained"], ["n5"])
        converted = _edition(
            [
                _event(
                    "n5c",
                    [_source("https://lab.example/plan", support=_support("original", "read", "The company expects to reach 1 million users by Q2."))],
                    lede="公司预计第二季度用户达到 100 万。",
                    claims=[{"kind": "predicted", "value": "100 万"}],
                    placement="lead",
                )
            ]
        )
        record = converted["events"][0]["editorial_verifications"][0]
        record["source_span"] = "The company expects to reach 1 million users by Q2."
        record["translated_claim"] = "公司预计第二季度用户达到 100 万"
        record["normalized_numbers"] = [{"source": "1 million", "claim": "100 万"}]
        converted.pop("content_hash", None)
        converted = validate_edition(converted)
        self.assertEqual(assess_new_candidate(converted)["retained"], ["n5c"])

    def test_translation_equality_negation_and_chinese_names(self):
        cookie = "Cookie policy. We use cookies to improve this website."
        excerpt = "The lab measured 83.0 tokens per second on its public benchmark."
        rejected_names = [
            "阿里发布通义千问新版，面向开发者开放下载。",
            "百度千帆平台更新，面向开发者开放下载。",
            "阿里通义万相升级，面向开发者开放下载。",
            "零一万物发布新模型，面向开发者开放下载。",
            "这次更新十分重要，面向开发者开放下载。",
            "官方提醒千万不要泄露密钥，面向开发者开放下载。",
        ]
        for lede in rejected_names:
            retained, _rejected = _assess(
                _event("u", [_source(URL, support=_support(excerpt="Source page says the model is 面向开发者开放下载 now."))], lede=lede, placement="lead")
            )
            self.assertEqual(retained, ["u"], lede)
        for lede in (
            "Meta 发布 Llama 3.1 70B，面向开发者开放下载。",
            "支持百万上下文，面向开发者开放下载。",
            "公司估值达三千亿美元，面向开发者开放下载。",
            "推理成本下降百分之二十，面向开发者开放下载。",
        ):
            retained, rejected = _assess(
                _event("u", [_source(URL, support=_support(excerpt="Source page says the model is 面向开发者开放下载 now."))], lede=lede, placement="lead")
            )
            self.assertEqual(retained, [])
            self.assertIn("numeric_claim_without_kind", rejected[0]["reasons"])
        negated = [
            "每秒 83.0 token，官方未预测。",
            "每秒 83.0 token，厂商尚未预计。",
            "每秒 83.0 token 从未被预测。",
            "这并不属于每秒 83.0 token 的预测。",
            "83.0 tokens per second was never a forecast.",
            "83.0 tokens/s isn't a forecast.",
            "每秒 83.0 token，别当成预测。",
            "每秒 83.0 token：不是预测。",
            "每秒 83.0 token（不是预测）。",
        ]
        for lede in negated:
            retained, rejected = _assess(
                _event("v", [_source(URL, support=_support(excerpt=excerpt))], lede=lede, claims=[{"kind": "predicted", "value": "83.0"}], placement="lead")
            )
            self.assertEqual(retained, [])
            self.assertIn("prediction_not_visible", rejected[0]["reasons"], lede)
        retained, rejected = _assess(
            _event(
                "v",
                [_source(URL, support=_support(excerpt=excerpt))],
                lede="公司不仅预计每秒 83.0 token。",
                claims=[{"kind": "predicted", "value": "83.0"}],
                placement="lead",
            )
        )
        self.assertEqual(rejected, [])
        self.assertEqual(retained, ["v"])

        def bridged(event_id, source_excerpt, lede, span, translated, pairs, claims=None):
            event = _event(
                event_id,
                [_source(URL, support=_support("original", "read", source_excerpt))],
                lede=lede,
                claims=claims,
                placement="lead",
            )
            document = _edition([event])
            record = document["events"][0]["editorial_verifications"][0]
            record["source_span"] = span
            record["translated_claim"] = translated
            if pairs is not None:
                record["normalized_numbers"] = pairs
            document.pop("content_hash", None)
            return validate_edition(document)

        mismatch = bridged(
            "t3",
            "The company expects to reach 1 million users by Q2.",
            "公司预计第二季度用户达到 300 万。",
            "expects to reach 1 million users",
            "预计第二季度用户达到 300 万",
            [{"source": "1 million", "claim": "300 万"}],
            [{"kind": "predicted", "value": "300 万"}],
        )
        self.assertEqual(assess_new_candidate(mismatch)["retained"], [])
        cookie_doc = bridged(
            "t2",
            cookie,
            "公司估值达 3000 亿美元，较上轮翻倍。",
            "We use cookies to improve",
            "公司估值达 3000 亿美元",
            [{"source": "anything", "claim": "3000 亿美元"}],
            [{"kind": "attributed", "value": "3000 亿美元"}],
        )
        self.assertEqual(assess_new_candidate(cookie_doc)["retained"], [])
        matched = bridged(
            "t3b",
            "The company expects to reach 1 million users by Q2.",
            "公司预计第二季度用户达到 100 万。",
            "expects to reach 1 million users",
            "预计第二季度用户达到 100 万",
            [{"source": "1 million", "claim": "100 万"}],
            [{"kind": "predicted", "value": "100 万"}],
        )
        self.assertEqual(assess_new_candidate(matched)["retained"], ["t3b"])
        plain = bridged(
            "t6",
            "Today we're bringing memory to Claude for Team and Enterprise users.",
            "Anthropic 为 Claude 团队版和企业版用户上线记忆功能，可在设置中开启。",
            "bringing memory to Claude for Team and Enterprise users",
            "为 Claude 团队版和企业版用户上线记忆功能",
            None,
        )
        self.assertEqual(assess_new_candidate(plain)["retained"], ["t6"])

    def test_grammatical_yi_is_not_a_quantity_and_real_amounts_are(self):
        excerpt = "Source page says the model is 面向开发者开放下载 now."
        grammatical = [
            "公司第一次公布这组数字，面向开发者开放下载。",
            "那是另一家公司的方案，面向开发者开放下载。",
            "审稿是一位没有出资的研究者，再加一次内审，面向开发者开放下载。",
            "页面并带了一个调参实验，面向开发者开放下载。",
            "他说这是第一款接入家庭的型号，面向开发者开放下载。",
        ]
        for lede in grammatical:
            retained, rejected = _assess(
                _event("gram", [_source(URL, support=_support(excerpt=excerpt))], lede=lede, placement="lead")
            )
            self.assertEqual(retained, ["gram"], (lede, rejected))
        for lede in (
            "推理速度升到原先的三倍，面向开发者开放下载。",
            "公司公布与六家药物机构合作，面向开发者开放下载。",
            "价格是每百万 token 两美元，面向开发者开放下载。",
            "调用失败少于十万分之一，面向开发者开放下载。",
            "性能提升两倍，面向开发者开放下载。",
            "市场份额达三成，面向开发者开放下载。",
            "成本降到十分之一，面向开发者开放下载。",
            "延迟降到二分之一，面向开发者开放下载。",
        ):
            retained, rejected = _assess(
                _event("qty", [_source(URL, support=_support(excerpt=excerpt))], lede=lede, placement="lead")
            )
            self.assertEqual(retained, [], lede)
            self.assertIn("numeric_claim_without_kind", rejected[0]["reasons"])

        from candidate_policy import _parse_quantity, _quantities_in, _unit_numbers

        real = {
            "2026-11-05": ("uber-agent-rides", ("第一次公布", "任何一家")),
            "2026-11-07": ("biobasics-bench", ("一位", "一次内审")),
            "2026-11-08": ("llama-moe-cache-cpu", ("一个调参",)),
        }
        for edition_id, (event_id, snippets) in real.items():
            document = json.loads((ROOT / "editions" / edition_id / "edition.json").read_text(encoding="utf-8"))
            event = next(item for item in document["events"] if item["id"] == event_id)
            text = "\n".join(
                [event.get("lede") or "", *[item for item in event["facts"] if isinstance(item, str)]]
            )
            for snippet in snippets:
                self.assertIn(snippet, text)
                self.assertEqual(_unit_numbers(snippet), set(), snippet)

        def bridged(event_id, source_excerpt, lede, span, translated, pairs, claims):
            document = _edition(
                [
                    _event(
                        event_id,
                        [_source(URL, support=_support("original", "read", source_excerpt))],
                        lede=lede,
                        claims=claims,
                        placement="lead",
                    )
                ]
            )
            record = document["events"][0]["editorial_verifications"][0]
            record["source_span"] = span
            record["translated_claim"] = translated
            record["normalized_numbers"] = pairs
            document.pop("content_hash", None)
            return validate_edition(document)

        trillion = bridged(
            "tri",
            "The market could reach 1.5 trillion dollars by 2030 overall.",
            "市场规模到 2030 年可达 1.5 万亿美元。",
            "reach 1.5 trillion dollars",
            "市场规模到 2030 年可达 1.5 万亿美元",
            [{"source": "1.5 trillion", "claim": "1.5 万亿美元"}],
            [{"kind": "attributed", "value": "1.5 万亿美元"}],
        )
        self.assertEqual(assess_new_candidate(trillion)["retained"], ["tri"])
        rounded = bridged(
            "rnd",
            "The market reached 1.48 trillion dollars in total revenue.",
            "市场规模约 1.5 万亿美元。",
            "reached 1.48 trillion dollars",
            "市场规模约 1.5 万亿美元",
            [{"source": "1.48 trillion", "claim": "1.5 万亿美元"}],
            [{"kind": "attributed", "value": "1.5 万亿美元"}],
        )
        self.assertEqual(assess_new_candidate(rounded)["retained"], ["rnd"])
        mixed = bridged(
            "mix",
            "The startup has 300 employees and raised 100 million dollars last week.",
            "这家公司有 300 人，融资 1 亿美元。",
            "has 300 employees and raised 100 million dollars",
            "这家公司有 300 人，融资 1 亿美元",
            [
                {"source": "300 employees", "claim": "300 人"},
                {"source": "100 million", "claim": "1 亿美元"},
            ],
            [{"kind": "attributed", "value": "300 人"}, {"kind": "attributed", "value": "1 亿美元"}],
        )
        self.assertEqual(assess_new_candidate(mixed)["retained"], ["mix"])
        self.assertEqual(_parse_quantity("1.5 trillion")[0], 1.5e12)
        self.assertEqual(
            set(_quantities_in("这家公司有 300 人，融资 1 亿美元。")),
            {(300.0, "absolute"), (1e8, "absolute")},
        )


if __name__ == "__main__":
    unittest.main()
