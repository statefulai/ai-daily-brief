#!/usr/bin/env python3
"""Validate production editions and build the static Pages directory."""

from __future__ import annotations

import argparse
from html import escape
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from edition import EDITION_ID_RE, EditionError, validate_edition  # noqa: E402
from outputs import render_web_edition  # noqa: E402

PRODUCTION_FILES = {"edition.json", "index.html"}
EDITION_PATH_RE = re.compile(
    r"^editions/(?P<edition_id>\d{4}-\d{2}-\d{2})/(?P<name>edition\.json|index\.html)$"
)


def _edition_directories(editions_dir: Path) -> list[Path]:
    if not editions_dir.exists():
        return []
    return sorted(
        path
        for path in editions_dir.iterdir()
        if path.is_dir() and not path.name.startswith(".")
    )


def validate_edition_directory(directory: Path) -> dict:
    """Validate one immutable public edition directory."""
    if not EDITION_ID_RE.fullmatch(directory.name):
        raise EditionError(f"invalid edition directory: {directory.name}")
    names = {path.name for path in directory.iterdir()}
    if names != PRODUCTION_FILES:
        raise EditionError(
            f"{directory}: expected only {sorted(PRODUCTION_FILES)}, got {sorted(names)}"
        )

    document = json.loads((directory / "edition.json").read_text(encoding="utf-8"))
    if "content_hash" not in document:
        raise EditionError(f"{directory}: edition.json must include content_hash")
    edition = validate_edition(document)
    if document["content_hash"] != edition["content_hash"]:
        raise EditionError(f"{directory}: content_hash does not match canonical payload")
    if edition["edition_id"] != directory.name:
        raise EditionError(f"{directory}: edition_id does not match directory")
    if edition["status"] != "published_candidate":
        raise EditionError(f"{directory}: public edition must be published_candidate")
    for event in edition["events"]:
        if not any(
            source["kind"] == "primary" and source["verified"]
            for source in event["sources"]
        ):
            raise EditionError(
                f"{directory}: event {event['id']} has no verified primary source"
            )

    expected_html = render_web_edition(edition)
    actual_html = (directory / "index.html").read_text(encoding="utf-8")
    if actual_html != expected_html:
        raise EditionError(f"{directory}: index.html is not the deterministic render")
    return edition


def validate_tree(editions_dir: Path) -> list[dict]:
    return [validate_edition_directory(path) for path in _edition_directories(editions_dir)]


def render_edition_file(path: Path) -> dict:
    """Normalize an agent-authored edition and write its deterministic HTML."""
    if path.name != "edition.json" or not path.is_file():
        raise EditionError("render input must be an existing edition.json")
    document = json.loads(path.read_text(encoding="utf-8"))
    edition = validate_edition(document)
    if edition["edition_id"] != path.parent.name:
        raise EditionError("edition_id does not match directory")
    if edition["status"] != "published_candidate":
        raise EditionError("only published_candidate editions can render HTML")
    for event in edition["events"]:
        if not any(
            source["kind"] == "primary" and source["verified"]
            for source in event["sources"]
        ):
            raise EditionError(
                f"{path.parent}: event {event['id']} has no verified primary source"
            )
    path.write_text(
        json.dumps(edition, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (path.parent / "index.html").write_text(
        render_web_edition(edition),
        encoding="utf-8",
    )
    return edition


def validate_changed_paths(
    paths: list[str], *, deleted_paths: list[str] | None = None
) -> None:
    """When a PR changes an edition, keep the change to one complete edition only."""
    normalized = [path.strip().lstrip("./") for path in paths if path.strip()]
    deleted = [path.strip().lstrip("./") for path in (deleted_paths or []) if path.strip()]
    if any(path.startswith("editions/") for path in deleted):
        raise EditionError("published edition history may not be deleted")
    edition_paths = [path for path in normalized if path.startswith("editions/")]
    if not edition_paths:
        return
    if len(edition_paths) != len(normalized):
        raise EditionError("an edition change may not include files outside editions/<date>/")

    matches = [EDITION_PATH_RE.fullmatch(path) for path in edition_paths]
    if any(match is None for match in matches):
        raise EditionError("edition changes may contain only edition.json and index.html")
    edition_ids = {match.group("edition_id") for match in matches if match}
    names = {match.group("name") for match in matches if match}
    if len(edition_ids) != 1:
        raise EditionError("one pull request may change only one edition date")
    if names != PRODUCTION_FILES:
        raise EditionError("an edition change must include edition.json and index.html")


def changed_paths(base: str, head: str) -> tuple[list[str], list[str]]:
    result = subprocess.run(
        [
            "git", "diff", "--no-renames", "--name-status",
            "--diff-filter=ACMRD", f"{base}...{head}",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    paths: list[str] = []
    deleted: list[str] = []
    for line in result.stdout.splitlines():
        status, path = line.split("\t", 1)
        paths.append(path)
        if status == "D":
            deleted.append(path)
    return paths, deleted


def _history_index(editions: list[dict]) -> str:
    ordered = sorted(editions, key=lambda edition: edition["edition_id"], reverse=True)
    links = "".join(
        f'<li><a href="editions/{escape(item["edition_id"])}/">'
        f'{escape(item["edition_id"])}</a></li>'
        for item in ordered
    )
    content = links or "<li>尚无公开期次</li>"
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI 日报 · 往期</title><style>
body{{margin:0;background:#e7e3db;color:#45463f;font:16px/1.8 -apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif}}
main{{width:min(720px,calc(100% - 32px));margin:32px auto;padding:32px;background:#faf7f0;border:1px solid #b9b4a9}}
h1{{margin:0 0 20px;color:#252621;font:400 38px/1.2 'Songti SC','STSong',serif}} ul{{margin:0;padding-left:22px}} a{{color:#91472f}}
</style></head><body><main><h1>AI 日报 · 往期</h1><ul>{content}</ul></main></body></html>
"""


def build_site(editions_dir: Path, output_dir: Path) -> list[dict]:
    editions = validate_tree(editions_dir)
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)
    published_root = output_dir / "editions"
    for edition in editions:
        source = editions_dir / edition["edition_id"]
        target = published_root / edition["edition_id"]
        target.mkdir(parents=True)
        (target / "edition.json").write_text(
            json.dumps(edition, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        shutil.copy2(source / "index.html", target / "index.html")
    (output_dir / "index.html").write_text(_history_index(editions), encoding="utf-8")
    return editions


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--editions", type=Path, default=Path("editions"))

    render_parser = subparsers.add_parser("render")
    render_parser.add_argument("--edition", type=Path, required=True)

    scope_parser = subparsers.add_parser("scope")
    scope_parser.add_argument("--base", required=True)
    scope_parser.add_argument("--head", required=True)

    build_parser = subparsers.add_parser("build")
    build_parser.add_argument("--editions", type=Path, default=Path("editions"))
    build_parser.add_argument("--output", type=Path, default=Path("_site"))

    args = parser.parse_args()
    if args.command == "validate":
        editions = validate_tree(args.editions)
        print(f"validated {len(editions)} edition(s)")
    elif args.command == "render":
        edition = render_edition_file(args.edition)
        print(f"rendered edition {edition['edition_id']}")
    elif args.command == "scope":
        paths, deleted = changed_paths(args.base, args.head)
        validate_changed_paths(paths, deleted_paths=deleted)
        print(f"validated change scope ({len(paths)} path(s))")
    else:
        editions = build_site(args.editions, args.output)
        print(f"built {len(editions)} edition(s) into {args.output}")


if __name__ == "__main__":
    main()
