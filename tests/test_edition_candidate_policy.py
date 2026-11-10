import json
import unittest
from pathlib import Path

from candidate_policy import (
    assess_new_candidate,
    evidence_outcome,
    enforcement_mode,
    record_reading,
    require_new_candidate,
    review_changed_edition,
)
from edition import EditionError, validate_edition
from scripts.edition_ci import validate_edition_directory

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def _source(url, *, kind="primary", support=None, verified=True, note=None):
    source = {
        "label": "来源",
        "url": url,
        "kind": kind,
        "published_at": "2026-11-11T01:00:00+00:00",
        "verified": verified,
        "note": note,
    }
    if support is not None:
        source["support"] = support
    return source


def _support(relationship="original", reading="read", excerpt="读者看到的导语与结果。"):
    return {"relationship": relationship, "reading": reading, "excerpt": excerpt}


def _event(event_id, sources, *, placement="story", claims=None, lede="读者看到的导语。"):
    event = {
        "id": event_id,
        "placement": placement,
        "window": "in_window",
        "title": event_id,
        "kicker": "焦点",
        "facts": ["正文里的事实。"],
        "conditions": [],
        "background": [],
        "lede": lede,
        "sources": sources,
    }
    if claims is not None:
        event["claims"] = claims
    return event


def _edition(events, edition_id="2026-11-12"):
    for event in events:
        for source in event["sources"]:
            support = source.get("support") or {}
            excerpt = support.get("excerpt") or ""
            if (
                support.get("relationship") == "original"
                and support.get("reading") == "read"
                and isinstance(excerpt, str)
                and len(excerpt) >= 8
            ):
                event.setdefault("editorial_verifications", []).append(
                    {
                        "edition_id": edition_id,
                        "attempt": 1,
                        "source_url": source["url"],
                        "excerpt": excerpt,
                        "claim": event.get("lede") or event["title"],
                        "verdict": "supports",
                    }
                )
    return validate_edition(
        {
            "schema_version": "1",
            "edition_id": edition_id,
            "timezone": "Asia/Shanghai",
            "cutoff_at": f"{edition_id}T08:00:00+08:00",
            "generated_at": f"{edition_id}T08:05:00+08:00",
            "attempt": 1,
            "status": "published_candidate",
            "sources": [{"id": "official", "status": "success", "item_count": len(events), "error": None}],
            "events": events,
            "contribution_count": 0,
            "failure": None,
        }
    )


def _load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class CandidatePolicyTest(unittest.TestCase):
    def test_original_pr_fixtures_fail_new_candidate_rules_and_name_ids(self):
        pr65 = _load("pr65-2026-11-07-edition.json")
        pr66 = _load("pr66-2026-11-08-edition.json")
        self.assertEqual(validate_edition(pr65)["status"], "published_candidate")
        self.assertEqual(validate_edition(pr66)["status"], "published_candidate")

        first = assess_new_candidate(pr65)
        second = assess_new_candidate(pr66)
        self.assertEqual(first["retained"], [])
        funding = next(item for item in first["rejected"] if item["id"] == "openai-funding-round")
        self.assertIn("missing_support", funding["reasons"])
        for event_id in ("nvidia-h200-new-matrix", "llama-moe-cache-cpu"):
            item = next(item for item in second["rejected"] if item["id"] == event_id)
            self.assertIn("missing_support", item["reasons"])
        with self.assertRaisesRegex(EditionError, "openai-funding-round: missing_support"):
            require_new_candidate(pr65)
        with self.assertRaisesRegex(EditionError, "nvidia-h200-new-matrix: missing_support"):
            require_new_candidate(pr66)

    def test_secondhand_primary_is_rejected_without_reading_the_note(self):
        denied = _edition(
            [
                _event(
                    "retold",
                    [
                        _source(
                            "https://example.com/retelling",
                            support=_support("retelling", "read", "A later article described the report."),
                            note="这不是转述",
                        )
                    ],
                    placement="lead",
                )
            ]
        )
        silent = _edition(
            [
                _event(
                    "silent",
                    [_source("https://example.com/page", support=None, note="普通补充")],
                    placement="lead",
                )
            ]
        )
        report = assess_new_candidate(denied)
        self.assertEqual(report["rejected"], [{"id": "retold", "reasons": ["no_read_original"]}])
        self.assertEqual(assess_new_candidate(silent)["rejected"][0]["reasons"], ["missing_support"])

    def test_read_original_keeps_a_secondary_retelling(self):
        document = _edition(
            [
                _event(
                    "kept",
                    [
                        _source(
                            "https://example.com/original",
                            support=_support(),
                        ),
                        _source(
                            "https://example.com/secondary",
                            kind="secondary",
                            support=_support("retelling", "read", "The second article retold the lab."),
                            verified=False,
                        ),
                    ],
                    placement="lead",
                )
            ]
        )
        report = assess_new_candidate(document)
        self.assertEqual(report, {"retained": ["kept"], "rejected": []})
        require_new_candidate(document)
        urls = [source["url"] for source in document["events"][0]["sources"]]
        self.assertEqual(
            urls,
            ["https://example.com/original", "https://example.com/secondary"],
        )

    def test_blocked_reads_cannot_become_support(self):
        self.assertEqual(record_reading(http_status=403, body_read=True), "http_403")
        self.assertEqual(record_reading(http_status=429), "http_429")
        self.assertEqual(record_reading(error="TimeoutError: timed out"), "timeout")
        self.assertEqual(record_reading(timestamp_only=True, body_read=False), "timestamps_only")
        self.assertEqual(record_reading(), "not_fetched")
        cases = {
            "http_403": record_reading(http_status=403),
            "http_429": record_reading(http_status=429),
            "timeout": record_reading(error="TimeoutError: timed out"),
            "timestamps_only": record_reading(timestamp_only=True),
            "not_fetched": record_reading(),
        }
        for reading in cases.values():
            document = _edition(
                [
                    _event(
                        "blocked",
                        [_source("https://example.com/original", support=_support("original", reading, None))],
                        placement="lead",
                    )
                ]
            )
            reasons = assess_new_candidate(document)["rejected"][0]["reasons"]
            self.assertIn(f"original_not_read:{reading}", reasons)

    def test_prediction_must_be_visible_outside_the_source_note(self):
        hidden = _edition(
            [
                _event(
                    "hidden-prediction",
                    [
                        _source(
                            "https://example.com/original",
                            support=_support(excerpt="作者写到每秒 83.0 token。"),
                            note="83.0 是预测",
                        )
                    ],
                    claims=[{"kind": "predicted", "value": "83.0"}],
                    lede="作者写到每秒 83.0 token。",
                    placement="lead",
                )
            ]
        )
        visible = _edition(
            [
                _event(
                    "visible-prediction",
                    [_source("https://example.com/original", support=_support(excerpt="每秒 83.0 token 是预测输出。"))],
                    claims=[{"kind": "predicted", "value": "83.0"}],
                    lede="每秒 83.0 token 是预测输出，不是这次实测。",
                    placement="lead",
                )
            ]
        )
        self.assertEqual(
            assess_new_candidate(hidden)["rejected"][0]["reasons"],
            ["prediction_not_visible"],
        )
        self.assertEqual(assess_new_candidate(visible)["retained"], ["visible-prediction"])
        split = _edition(
            [
                _event(
                    "split",
                    [_source("https://example.com/original", support=_support())],
                    claims=[{"kind": "predicted", "value": "83.0"}],
                    lede="这是预测。",
                    placement="lead",
                )
            ]
        )
        split["events"][0]["facts"] = ["作者写到每秒 83.0 token。"]
        self.assertIn(
            "prediction_not_visible",
            assess_new_candidate(split)["rejected"][0]["reasons"],
        )

    def test_self_reported_support_does_not_prove_the_claim(self):
        unrelated = _edition(
            [
                _event(
                    "gap-a",
                    [
                        _source(
                            "https://example.com/original",
                            support=_support("original", "read", "unrelated boilerplate"),
                        )
                    ],
                    placement="lead",
                )
            ]
        )
        disguised = _edition(
            [
                _event(
                    "disguised",
                    [
                        _source(
                            "https://example.com/retelling",
                            support=_support("original", "read", "A later article described the number."),
                        )
                    ],
                    placement="lead",
                )
            ]
        )
        measured = _edition(
            [
                _event(
                    "measured-label",
                    [_source("https://example.com/original", support=_support())],
                    claims=[{"kind": "measured", "value": "83.0"}],
                    lede="每秒 83.0 token 是预测输出。",
                    placement="lead",
                )
            ]
        )
        self.assertIn("excerpt_unrelated", assess_new_candidate(unrelated)["rejected"][0]["reasons"])
        self.assertEqual(assess_new_candidate(unrelated)["rejected"][0]["id"], "gap-a")
        self.assertIn("excerpt_unrelated", assess_new_candidate(disguised)["rejected"][0]["reasons"])
        self.assertIn(
            "measured_describes_prediction",
            assess_new_candidate(measured)["rejected"][0]["reasons"],
        )
        false_attestation = _edition(
            [
                _event(
                    "false-attestation",
                    [
                        _source(
                            "https://news.example/retell",
                            support=_support("original", "read", "据外媒报道该公司宣布融资。"),
                        )
                    ],
                    lede="据外媒报道该公司宣布融资。",
                    placement="lead",
                )
            ]
        )
        self.assertEqual(
            assess_new_candidate(false_attestation)["retained"],
            ["false-attestation"],
        )

    def test_mixed_file_names_both_sides_and_still_rejects_the_file(self):
        document = _edition(
            [
                _event(
                    "kept",
                    [_source("https://example.com/original", support=_support())],
                    placement="lead",
                ),
                _event(
                    "dropped",
                    [_source("https://example.com/retelling", support=_support("retelling", "read", "Later copy."))],
                ),
            ]
        )
        report = assess_new_candidate(document)
        self.assertEqual(report["retained"], ["kept"])
        self.assertEqual(report["rejected"], [{"id": "dropped", "reasons": ["no_read_original"]}])
        with self.assertRaisesRegex(EditionError, "dropped: no_read_original"):
            require_new_candidate(document)
        self.assertEqual(len(document["events"]), 2)

    def test_evidence_failure_is_not_an_empty_news_day(self):
        sources = [{"id": "official", "status": "success", "item_count": 1, "error": None}]
        self.assertEqual(evidence_outcome(sources, retained=0, rejected=2), "failed")
        self.assertEqual(evidence_outcome(sources, retained=0, rejected=0), "no_new_value")
        self.assertEqual(
            evidence_outcome(
                [{"id": "official", "status": "failed", "item_count": 0, "error": "timeout"}],
                retained=0,
                rejected=0,
            ),
            "failed",
        )

    def test_unchanged_history_is_not_a_backdoor_for_a_new_draft(self):
        path = ROOT / "editions" / "2026-11-08" / "edition.json"
        text = path.read_text(encoding="utf-8")
        historical = json.loads(text)
        self.assertEqual(validate_edition(historical)["content_hash"], historical["content_hash"])
        validate_edition_directory(path.parent)
        self.assertEqual(review_changed_edition(text, text), "historical")
        self.assertEqual(
            enforcement_mode(changed=False, matches_baseline=True),
            "historical",
        )
        self.assertEqual(enforcement_mode(changed=True, matches_baseline=False), "new")
        drafted = dict(historical)
        drafted["historical_exemption"] = True
        mutated = json.dumps(drafted, ensure_ascii=False)
        with self.assertRaisesRegex(EditionError, "published edition is frozen"):
            review_changed_edition(text, mutated)
        with self.assertRaisesRegex(EditionError, "missing_support"):
            require_new_candidate(historical)
