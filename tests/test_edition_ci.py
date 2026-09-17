import json
import tempfile
import unittest
from pathlib import Path

from edition import EditionError, build_edition, write_edition
from outputs import write_web_edition
from scripts.edition_ci import (
    build_site,
    render_edition_file,
    validate_changed_paths,
    validate_tree,
)


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


if __name__ == "__main__":
    unittest.main()
