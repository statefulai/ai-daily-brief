#!/usr/bin/env python3
"""Validate production editions and build the static Pages directory."""

from __future__ import annotations

import argparse
from datetime import datetime
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
from outputs import WEEKDAYS_ZH, _edition_note, render_web_edition  # noqa: E402

MASTHEAD_NAME = "masthead-art.webp"
_HOME_TOKENS = ("TITLE", "DESCRIPTION", "LATEST", "ARCHIVE")

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


def _masthead_path() -> Path:
    return ROOT / "templates" / "assets" / MASTHEAD_NAME


def _assert_safe_output(editions_dir: Path, output_dir: Path) -> None:
    """Refuse to delete the repo, edition history, templates, or reference files."""
    output = output_dir.resolve()
    protected = [
        editions_dir.resolve(),
        (ROOT / "editions").resolve(),
        (ROOT / "templates").resolve(),
        (ROOT / "uploads").resolve(),
    ]
    if output == ROOT.resolve() or any(
        output == tree or tree in output.parents for tree in protected
    ):
        raise EditionError(
            "refusing to publish a homepage build into the repository root, "
            "editions, templates, or reference files"
        )


def _fill_home_template(values: dict[str, str]) -> str:
    content = (ROOT / "templates" / "home.html").read_text(encoding="utf-8")
    parts = re.split(r"\{\{([A-Z0-9_]+)\}\}", content)
    rendered: list[str] = []
    seen: list[str] = []
    for index, part in enumerate(parts):
        if index % 2 == 0:
            rendered.append(part)
            continue
        if part not in values:
            raise EditionError(f"unresolved home template field: {part}")
        seen.append(part)
        rendered.append(values[part])
    if len(seen) != len(_HOME_TOKENS) or set(seen) != set(_HOME_TOKENS):
        raise EditionError(f"home template fields mismatch: {seen}")
    return "".join(rendered)


def _edition_day(edition_id: str) -> datetime:
    return datetime.strptime(edition_id, "%Y-%m-%d")


def _select_lead(events: list[dict]) -> tuple[int, dict]:
    for index, event in enumerate(events, start=1):
        if event["placement"] == "lead":
            return index, event
    return 1, events[0]


def _conditions_html(conditions: list[str]) -> str:
    if not conditions:
        return ""
    items = "".join(f"<li>{escape(item)}</li>" for item in conditions)
    return (
        '<details class="scope"><summary>本条适用范围与核验边界</summary>'
        f"<ul>{items}</ul></details>"
    )


def _sidebar_html(edition_id: str, events: list[dict], lead: dict) -> str:
    others = [
        (index, event)
        for index, event in enumerate(events, start=1)
        if event is not lead
    ]
    if not others:
        return ""
    items = []
    for index, event in others[:3]:
        href = escape(f"editions/{edition_id}/#event-{index}", quote=True)
        items.append(
            f'<li><a href="{href}">'
            f'<span class="ordinal" aria-hidden="true">{index:02d}</span>'
            f"<h4>{escape(event['title'])}</h4></a></li>"
        )
    remaining = len(others) - 3
    note = (
        f'<p class="remaining-note">另有 {remaining} 条内容，见完整一期。</p>'
        if remaining > 0
        else ""
    )
    return (
        '<aside class="edition-contents" aria-labelledby="contents-heading">'
        '<h3 id="contents-heading">本期还包括</h3>'
        f'<ol class="contents-list">{"".join(items)}</ol>{note}</aside>'
    )


def _latest_html(edition: dict) -> str:
    edition_id = edition["edition_id"]
    day = _edition_day(edition_id)
    _, lead = _select_lead(edition["events"])
    sidebar = _sidebar_html(edition_id, edition["events"], lead)
    layout = "lead-layout lead-layout--single" if not sidebar else "lead-layout"
    read_href = escape(f"editions/{edition_id}/", quote=True)
    meta_date = escape(f"{day:%Y.%m.%d} · {WEEKDAYS_ZH[day.weekday()]}")
    return (
        '<div class="section-heading"><div class="section-label">'
        '<h2 id="latest-heading">最新一期</h2>'
        '<span class="section-en" lang="en">LATEST EDITION</span></div>'
        f'<div class="edition-meta"><time datetime="{escape(edition_id, quote=True)}">'
        f"{meta_date}</time><span>{len(edition['events'])} 条内容</span></div></div>"
        f'<p class="coverage-note">{escape(_edition_note(edition))}</p>'
        f'<div class="{layout}"><article class="lead-story" aria-labelledby="lead-heading">'
        f'<p class="kicker">{escape(lead["kicker"])}</p>'
        f'<h3 id="lead-heading">{escape(lead["title"])}</h3>'
        f'<div class="lead-actions"><a class="read-edition" href="{read_href}">'
        '阅读完整一期 <span aria-hidden="true">→</span></a></div>'
        '<p class="excerpt-label">头条摘录</p>'
        f'<p class="excerpt">{escape(lead["facts"][0])}</p>'
        f"{_conditions_html(lead.get('conditions') or [])}</article>{sidebar}</div>"
    )


def _empty_latest_html() -> str:
    return (
        '<div class="section-heading"><div class="section-label">'
        '<h2 id="latest-heading">最新一期</h2>'
        '<span class="section-en" lang="en">LATEST EDITION</span></div></div>'
        '<p class="coverage-note">尚无公开期次</p>'
    )


def _archive_html(older: list[dict], published_count: int) -> str:
    prior = max(0, published_count - 1)
    parts = [
        '<div class="section-heading"><div class="section-label">'
        '<h2 id="archive-heading">往期</h2>'
        '<span class="section-en" lang="en">ARCHIVE</span></div>'
        f'<span class="archive-count">此前已刊 {prior} 期</span></div>'
    ]
    if published_count == 0:
        parts.append('<p class="coverage-note">尚无公开期次</p>')
        return "".join(parts)
    if not older:
        parts.append('<p class="coverage-note">暂无更早期次</p>')
        return "".join(parts)

    groups: list[tuple[str, list[str]]] = []
    for edition in older:
        day = _edition_day(edition["edition_id"])
        month_label = f"{day.year} 年 {day.month} 月"
        if not groups or groups[-1][0] != month_label:
            groups.append((month_label, []))
        edition_id = edition["edition_id"]
        _, lead = _select_lead(edition["events"])
        href = escape(f"editions/{edition_id}/", quote=True)
        groups[-1][1].append(
            f'<li><a class="archive-link" href="{href}">'
            f'<time datetime="{escape(edition_id, quote=True)}">'
            f"{escape(f'{day.month:02d} 月 {day.day:02d} 日')}</time>"
            f"<h3>{escape(lead['title'])}</h3>"
            f'<span class="item-count">{len(edition["events"])} 条内容</span>'
            '<span class="archive-arrow" aria-hidden="true">→</span></a></li>'
        )
    for label, items in groups:
        parts.append(f'<p class="archive-month">{escape(label)}</p>')
        parts.append(f'<ol class="archive-list">{"".join(items)}</ol>')
    return "".join(parts)


def _home_description(latest: dict | None) -> str:
    base = "AI 日报公开首页：最新一期、往期与编选原则。"
    if latest is None:
        return base + "尚无公开期次。"
    day = _edition_day(latest["edition_id"])
    return (
        f"{base}最新一期为 {day.year} 年 {day.month} 月 {day.day} 日，"
        f"共 {len(latest['events'])} 条内容。"
    )


def _history_index(editions: list[dict]) -> str:
    ordered = sorted(editions, key=lambda edition: edition["edition_id"], reverse=True)
    latest = ordered[0] if ordered else None
    return _fill_home_template(
        {
            "TITLE": escape("AI 日报 · 最新一期与往期"),
            "DESCRIPTION": escape(_home_description(latest), quote=True),
            "LATEST": _latest_html(latest) if latest else _empty_latest_html(),
            "ARCHIVE": _archive_html(ordered[1:], len(ordered)),
        }
    )


def build_site(editions_dir: Path, output_dir: Path) -> list[dict]:
    editions = validate_tree(editions_dir)
    masthead = _masthead_path()
    if not masthead.is_file():
        raise EditionError(f"homepage masthead is missing: {masthead}")
    _assert_safe_output(editions_dir, output_dir)
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
    assets = output_dir / "assets"
    assets.mkdir()
    shutil.copyfile(masthead, assets / MASTHEAD_NAME)
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
