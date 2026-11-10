import fcntl
import json
import tempfile
import unittest
from pathlib import Path

from edition import EditionError, validate_edition
from revision import RevisionWorkspace
from tests.test_edition_candidate_policy import _edition, _event, _source, _support

HEAD_A = "a" * 40
HEAD_B = "b" * 40
HEAD_C = "c" * 40
HEAD_D = "d" * 40


def _write_workspace(root: Path, document: dict) -> None:
    (root / "edition.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _contract(document, **overrides):
    contract = {
        "edition_id": document["edition_id"],
        "attempt": document["attempt"],
        "revision": 1,
        "base_head": HEAD_A,
        "base_content_hash": document["content_hash"],
        "approved_removals": [],
        "approved_wording": [],
        "execution_evidence": None,
    }
    contract.update(overrides)
    return contract


class RevisionTest(unittest.TestCase):
    def _mixed(self):
        return _edition(
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

    def test_apply_removes_only_the_rejected_event_and_keeps_attempt(self):
        document = self._mixed()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_workspace(root, document)
            result = RevisionWorkspace(root).apply(
                _contract(document, approved_removals=["dropped"]),
                current_head=HEAD_A,
            )
            written = json.loads((root / "edition.json").read_text(encoding="utf-8"))
            html = (root / "index.html").read_text(encoding="utf-8")

        self.assertEqual(result["edition_id"], document["edition_id"])
        self.assertEqual(result["attempt"], 1)
        self.assertEqual(written["attempt"], 1)
        self.assertEqual([event["id"] for event in written["events"]], ["kept"])
        self.assertNotEqual(result["content_hash"], document["content_hash"])
        self.assertIn("kept", html)
        self.assertNotIn("dropped", html)
        self.assertEqual(result["platform_continuation"], "blocked")
        self.assertIsNone(result["execution_id"])
        self.assertEqual(result["execution_block_reason"], "missing_reliable_execution_evidence")

    def test_agent_id_and_local_token_are_not_execution_ids(self):
        document = self._mixed()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_workspace(root, document)
            result = RevisionWorkspace(root).apply(
                _contract(
                    document,
                    approved_removals=["dropped"],
                    execution_evidence="bc-5968f500-228c-5e05-938e-0233cf57c6aa",
                ),
                current_head=HEAD_A,
            )
            _write_workspace(root, document)
            (root / "revision-state.json").unlink()
            local = RevisionWorkspace(root).apply(
                _contract(
                    document,
                    revision=2,
                    approved_removals=["dropped"],
                    execution_evidence="11111111-2222-4333-8444-555555555555",
                ),
                current_head=HEAD_A,
            )
        self.assertIsNone(result["execution_id"])
        self.assertEqual(result["execution_block_reason"], "agent_id_is_not_execution_id")
        self.assertNotIn("bc-5968f500", json.dumps(result))
        self.assertIsNone(local["execution_id"])
        self.assertEqual(local["execution_block_reason"], "unverified_execution_evidence")
        self.assertNotIn("11111111-2222-4333-8444-555555555555", json.dumps(local))

    def test_stale_head_does_not_write(self):
        document = self._mixed()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_workspace(root, document)
            before = (root / "edition.json").read_text(encoding="utf-8")
            with self.assertRaisesRegex(EditionError, "stale revision head"):
                RevisionWorkspace(root).apply(
                    _contract(document, approved_removals=["dropped"]),
                    current_head=HEAD_B,
                )
            self.assertEqual((root / "edition.json").read_text(encoding="utf-8"), before)
            self.assertFalse((root / "index.html").exists())
            state = json.loads((root / "revision-state.json").read_text(encoding="utf-8"))
            key = f"{document['edition_id']}|{document['attempt']}|1"
            self.assertEqual(state["completions"][key]["status"], "blocked")
            self.assertEqual(state["completions"][key]["contract"]["attempt"], document["attempt"])
            with self.assertRaisesRegex(EditionError, "already reserved"):
                RevisionWorkspace(root).apply(
                    _contract(document, approved_removals=["dropped"]),
                    current_head=HEAD_A,
                )
            self.assertEqual((root / "edition.json").read_text(encoding="utf-8"), before)

    def test_duplicate_completion_does_not_apply_twice(self):
        document = self._mixed()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_workspace(root, document)
            workspace = RevisionWorkspace(root)
            contract = _contract(document, approved_removals=["dropped"])
            first = workspace.apply(contract, current_head=HEAD_A)
            second = workspace.apply(contract, current_head=HEAD_A)
            self.assertEqual(first["content_hash"], second["content_hash"])
            written = json.loads((root / "edition.json").read_text(encoding="utf-8"))
            self.assertEqual(written["content_hash"], first["content_hash"])

    def test_lock_failure_and_revision_bound(self):
        document = self._mixed()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_workspace(root, document)
            lock_path = root / "revision.lock"
            with lock_path.open("a+", encoding="utf-8") as held:
                fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(EditionError, "revision writer is already running"):
                    RevisionWorkspace(root).apply(
                        _contract(document, approved_removals=["dropped"]),
                        current_head=HEAD_A,
                    )
                self.assertFalse((root / "revision-state.json").exists())
                fcntl.flock(held.fileno(), fcntl.LOCK_UN)
            with self.assertRaisesRegex(EditionError, "outside the bound of 2"):
                RevisionWorkspace(root).apply(
                    _contract(document, revision=3, approved_removals=["dropped"]),
                    current_head=HEAD_A,
                )

    def test_refuses_to_remove_an_event_the_policy_kept(self):
        document = self._mixed()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_workspace(root, document)
            with self.assertRaisesRegex(EditionError, "refusing to remove"):
                RevisionWorkspace(root).apply(
                    _contract(document, approved_removals=["kept"]),
                    current_head=HEAD_A,
                )

    def test_wording_clarification_makes_the_prediction_visible(self):
        document = _edition(
            [
                _event(
                    "numbers",
                    [_source("https://example.com/original", support=_support())],
                    claims=[{"kind": "predicted", "value": "83.0"}],
                    lede="作者写到每秒 83.0 token。",
                    placement="lead",
                )
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_workspace(root, document)
            result = RevisionWorkspace(root).apply(
                _contract(
                    document,
                    approved_wording=[
                        {
                            "event_id": "numbers",
                            "field": "lede",
                            "value": "每秒 83.0 token 是预测输出，不是这次实测。",
                        }
                    ],
                    editorial_rejections=[
                        {
                            "id": "numbers",
                            "reasons": ["the prediction is not visible to the reader"],
                            "edition_id": document["edition_id"],
                            "attempt": document["attempt"],
                            "base_head": HEAD_A,
                        }
                    ],
                ),
                current_head=HEAD_A,
            )
            written = json.loads((root / "edition.json").read_text(encoding="utf-8"))
            html = (root / "index.html").read_text(encoding="utf-8")
        self.assertEqual(written["attempt"], document["attempt"])
        self.assertIn("预测输出", written["events"][0]["lede"])
        self.assertIn("预测输出", html)
        self.assertEqual(result["status"], "published_candidate")

    def test_removing_every_rejected_event_is_failed_not_no_new_value(self):
        document = _edition(
            [
                _event(
                    "one",
                    [_source("https://example.com/a", support=_support("retelling", "read", "Copy."))],
                    placement="lead",
                ),
                _event(
                    "two",
                    [_source("https://example.com/b", support=_support("retelling", "read", "Copy two."))],
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_workspace(root, document)
            result = RevisionWorkspace(root).apply(
                _contract(document, approved_removals=["one", "two"]),
                current_head=HEAD_A,
            )
            written = json.loads((root / "edition.json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(written["status"], "failed")
        self.assertEqual(written["failure"]["stage"], "evidence")
        self.assertNotEqual(written["status"], "no_new_value")
        self.assertEqual(written["events"], [])
        self.assertFalse((root / "index.html").exists())

    def test_final_review_is_bound_to_the_reviewed_head(self):
        document = self._mixed()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_workspace(root, document)
            workspace = RevisionWorkspace(root)
            result = workspace.apply(
                _contract(document, approved_removals=["dropped"]),
                current_head=HEAD_A,
            )
            bound = workspace.bind_reviewed_head(
                edition_id=result["edition_id"],
                attempt=result["attempt"],
                revision=1,
                reviewed_head=HEAD_C,
                content_hash=result["content_hash"],
            )
            again = workspace.bind_reviewed_head(
                edition_id=result["edition_id"],
                attempt=result["attempt"],
                revision=1,
                reviewed_head=HEAD_C,
                content_hash=result["content_hash"],
            )
            self.assertEqual(bound["reviewed_head"], HEAD_C)
            self.assertEqual(again["reviewed_head"], HEAD_C)
            self.assertEqual(bound["platform_continuation"], "blocked")
            with self.assertRaisesRegex(EditionError, "bound final head"):
                workspace.bind_reviewed_head(
                    edition_id=result["edition_id"],
                    attempt=result["attempt"],
                    revision=1,
                    reviewed_head=HEAD_D,
                    content_hash=result["content_hash"],
                )

    def test_validated_failed_edition_has_no_events(self):
        document = self._mixed()
        failed = dict(document)
        failed["events"] = []
        failed["status"] = "failed"
        failed["failure"] = {"stage": "evidence", "message": "evidence rejected every event: dropped"}
        failed.pop("content_hash", None)
        validated = validate_edition(failed)
        self.assertEqual(validated["status"], "failed")
        self.assertEqual(validated["events"], [])

    def test_label_branch_and_short_sha_are_not_git_heads(self):
        document = self._mixed()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_workspace(root, document)
            before = (root / "edition.json").read_text(encoding="utf-8")
            with self.assertRaisesRegex(EditionError, "40-digit hexadecimal git head"):
                RevisionWorkspace(root).apply(
                    _contract(document, base_head="head-a", approved_removals=["dropped"]),
                    current_head="head-a",
                )
            for label in ("main", "a" * 12):
                with self.assertRaisesRegex(EditionError, "already reserved"):
                    RevisionWorkspace(root).apply(
                        _contract(document, base_head=label, approved_removals=["dropped"]),
                        current_head=label,
                    )
            self.assertEqual((root / "edition.json").read_text(encoding="utf-8"), before)
            state = json.loads((root / "revision-state.json").read_text(encoding="utf-8"))
            self.assertEqual(
                state["completions"][f"{document['edition_id']}|1|1"]["status"],
                "blocked",
            )
            with self.assertRaisesRegex(EditionError, "40-digit hexadecimal git head"):
                RevisionWorkspace(root).apply(
                    _contract(
                        document,
                        revision=2,
                        base_head="a" * 12,
                        approved_removals=["dropped"],
                    ),
                    current_head="a" * 12,
                )

    def test_wording_cannot_change_a_retained_event_without_editorial_rejection(self):
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
            before = (root / "edition.json").read_text(encoding="utf-8")
            with self.assertRaisesRegex(EditionError, "editorial rejection"):
                RevisionWorkspace(root).apply(
                    _contract(
                        document,
                        approved_wording=[
                            {"event_id": "kept", "field": "lede", "value": "未授权改写。"}
                        ],
                    ),
                    current_head=HEAD_A,
                )
            self.assertEqual((root / "edition.json").read_text(encoding="utf-8"), before)
            for source in json.loads(before)["events"][0]["sources"]:
                self.assertEqual(source["kind"], "primary")

    def test_editor_can_reject_a_structurally_valid_event(self):
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
            result = RevisionWorkspace(root).apply(
                _contract(
                    document,
                    approved_removals=["kept"],
                    editorial_rejections=[
                        {
                            "id": "kept",
                            "reasons": ["editor declined this structurally valid event"],
                            "edition_id": document["edition_id"],
                            "attempt": document["attempt"],
                            "base_head": HEAD_A,
                        }
                    ],
                ),
                current_head=HEAD_A,
            )
            written = json.loads((root / "edition.json").read_text(encoding="utf-8"))
            self.assertFalse((root / "index.html").exists())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(written["failure"]["stage"], "editorial")
        self.assertEqual(written["attempt"], document["attempt"])
        self.assertEqual(written["events"], [])
