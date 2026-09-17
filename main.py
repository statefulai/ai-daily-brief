#!/usr/bin/env python3
"""
AI News Aggregator - Main Entry Point

Usage:
    python main.py                  # full pipeline
    python main.py --sources-only   # fetch sources only (no LLM)
    python main.py --dry-run        # print edition.json, don't write files
"""

import asyncio
import argparse
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv

# Load .env file (won't override existing env vars)
load_dotenv()

from sources import (
    NewsItem,
    fetch_hackernews,
    fetch_github_trending,
    fetch_huggingface,
    fetch_rss_feeds,
    fetch_ruanyf_weekly,
    fetch_reddit,
)
from summarizer import CurationError, curate_daily_brief
from outputs import (
    format_daily_brief,
    push_to_notion,
    write_archive,
    write_readme,
    write_web_edition,
)
from source_status import SourceResult, run_source
from curate import assemble_curated_events
from verify import verify_event_primary_sources
from edition import (
    acquire_edition_lock,
    build_edition,
    edition_id_for,
    edition_window,
    next_attempt,
    release_edition_lock,
    write_edition,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ai-news")


def load_config(path: str = "config.yaml") -> dict:
    """Load configuration from YAML file."""
    config_path = Path(path)
    if not config_path.exists():
        logger.warning(f"Config file {path} not found, using defaults")
        return {}
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def filter_by_window(
    items: list[NewsItem],
    start_at: datetime,
    cutoff_at: datetime,
) -> list[NewsItem]:
    """Keep items published in the half-open interval [start, cutoff)."""
    if start_at.tzinfo is None or cutoff_at.tzinfo is None:
        raise ValueError("edition window datetimes must include a timezone")
    selected = []
    for item in items:
        if item.published.tzinfo is None:
            logger.warning("Skipping item with timezone-free published time: %s", item.url)
            continue
        if start_at <= item.published < cutoff_at:
            selected.append(item)
    return selected


def filter_by_keywords(
    items: list[NewsItem],
    include: list[str],
    exclude: list[str],
) -> list[NewsItem]:
    """Keyword-based pre-filter before LLM."""
    filtered = []
    for item in items:
        text = f"{item.title} {item.summary} {' '.join(item.tags)}".lower()

        # Check exclude first
        if any(kw in text for kw in exclude):
            continue

        # Check include (at least one keyword must match)
        if include and not any(kw in text for kw in include):
            continue

        filtered.append(item)
    return filtered


def deduplicate(items: list[NewsItem]) -> list[NewsItem]:
    """Remove duplicate items by URL."""
    seen_urls: set[str] = set()
    unique = []
    for item in items:
        normalized = item.url.rstrip("/").lower()
        if normalized not in seen_urls:
            seen_urls.add(normalized)
            unique.append(item)
    return unique


def load_previous_urls(archive_dir: str = "archives", lookback_days: int = 2) -> set[str]:
    """Load URLs from recent archive files to enable cross-day dedup."""
    from datetime import datetime, timedelta, timezone

    BEIJING_TZ = timezone(timedelta(hours=8))
    urls: set[str] = set()
    archive_path = Path(archive_dir)
    if not archive_path.exists():
        return urls

    today = datetime.now(BEIJING_TZ)
    for delta in range(1, lookback_days + 1):
        date_str = (today - timedelta(days=delta)).strftime("%Y-%m-%d")
        md_file = archive_path / f"{date_str}.md"
        if md_file.exists():
            try:
                content = md_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as e:
                logger.warning(f"Failed to read archive {md_file}: {e}")
                continue
            # Match markdown links, handling URLs that may contain balanced parentheses
            for match in re.finditer(r'\[.*?\]\((https?://(?:[^\s\(\)]+|\([^\)]*\))*)\)', content):
                urls.add(match.group(1).rstrip("/").lower())

    return urls


def cross_day_dedup(items: list[NewsItem], previous_urls: set[str]) -> list[NewsItem]:
    """Remove items that were already published in previous days."""
    if not previous_urls:
        return items
    filtered = []
    removed = 0
    for item in items:
        normalized = item.url.rstrip("/").lower()
        if normalized in previous_urls:
            removed += 1
        else:
            filtered.append(item)
    if removed:
        logger.info(f"Cross-day dedup removed {removed} previously published items")
    return filtered


SOURCE_SPECS = (
    ("hackernews", "HackerNews", fetch_hackernews),
    ("github_trending", "GitHub", fetch_github_trending),
    ("huggingface", "HuggingFace", fetch_huggingface),
    ("rss_feeds", "RSS", fetch_rss_feeds),
    ("ruanyf_weekly", "阮一峰周刊", fetch_ruanyf_weekly),
    ("reddit", "Reddit", fetch_reddit),
)


async def fetch_all_source_results(config: dict) -> list[SourceResult]:
    """Fetch every configured source and keep success / empty / failure distinct."""
    sources_cfg = config.get("sources", {})
    tasks = [
        run_source(source_id, name, fetcher, sources_cfg.get(source_id, {}))
        for source_id, name, fetcher in SOURCE_SPECS
    ]
    return list(await asyncio.gather(*tasks))


async def fetch_all_sources(config: dict) -> list[NewsItem]:
    """Fetch from all configured sources concurrently."""
    all_items: list[NewsItem] = []
    for result in await fetch_all_source_results(config):
        if result.status == "failed":
            logger.error(f"{result.name} source failed: {result.error}")
        all_items.extend(result.items)
    return all_items


def load_grok_contributions(config: dict) -> list[dict]:
    """Load the optional Grok Bot adapter input; local users need no such file."""
    path = (
        config.get("integrations", {})
        .get("grok_bot", {})
        .get("contributions_path")
    )
    if not path:
        return []
    contribution_path = Path(path)
    if not contribution_path.exists():
        logger.info("Optional Grok Bot contribution file is absent: %s", path)
        return []
    payload = json.loads(contribution_path.read_text(encoding="utf-8"))
    records = payload.get("items", payload) if isinstance(payload, dict) else payload
    from integrations.grok_bot.contributions import validate_contributions

    return validate_contributions(records)


async def write_legacy_outputs(curation_result: dict, output_cfg: dict) -> int:
    """Update the legacy Markdown path only after a valid curation result."""
    candidates = curation_result.get("candidates", [])
    brief = curation_result.get("brief", {})
    focus = brief.get("focus", {}) if isinstance(brief, dict) else {}
    focus_index = focus.get("index") if isinstance(focus, dict) else None
    if (
        not isinstance(candidates, list)
        or type(focus_index) is not int
        or not 0 <= focus_index < len(candidates)
    ):
        logger.info("Legacy outputs unchanged: no valid curated brief")
        return 0

    if output_cfg.get("github_readme", {}).get("enabled", True):
        readme_content = format_daily_brief(
            curation_result, output_cfg.get("github_readme", {})
        )
        readme_path = output_cfg.get("github_readme", {}).get(
            "file", "daily-brief.md"
        )
        write_readme(readme_content, readme_path)
    write_archive(curation_result, output_cfg.get("archive", {}))
    await push_to_notion(candidates[:20], output_cfg.get("notion", {}))
    return 1 + len(brief.get("highlights", [])) + len(brief.get("tools", []))


async def run(args: argparse.Namespace):
    """Main pipeline."""
    config = load_config(args.config)
    window_start, cutoff = edition_window()
    edition_id = edition_id_for(cutoff)

    # 1. Fetch all sources
    logger.info("=== Fetching sources ===")
    source_results = await fetch_all_source_results(config)
    raw_items = [item for result in source_results for item in result.items]
    for result in source_results:
        logger.info(
            f"{result.name}: {result.status}"
            + (f" ({result.error})" if result.error else f" items={len(result.items)}")
        )
    logger.info(f"Total raw items: {len(raw_items)}")

    # 2. Deduplicate (within same run)
    items = deduplicate(raw_items)
    logger.info(f"After dedup: {len(items)}")

    # 2b. Cross-day dedup (remove items already published yesterday)
    archive_dir = config.get("output", {}).get("archive", {}).get("directory", "archives")
    previous_urls = load_previous_urls(archive_dir, lookback_days=2)
    items = cross_day_dedup(items, previous_urls)
    logger.info(f"After cross-day dedup: {len(items)}")

    # 3. Keep the latest closed Beijing-time daily window.
    filter_cfg = config.get("filter", {})
    items = filter_by_window(items, window_start, cutoff)
    logger.info(
        "After edition window filter [%s, %s): %s",
        window_start.isoformat(),
        cutoff.isoformat(),
        len(items),
    )

    # 4. Keyword pre-filter
    keywords = filter_cfg.get("keywords", {})
    items = filter_by_keywords(
        items,
        include=keywords.get("include", []),
        exclude=keywords.get("exclude", []),
    )
    logger.info(f"After keyword filter: {len(items)}")

    if args.sources_only:
        print(json.dumps([result.to_record() for result in source_results], ensure_ascii=False, indent=2))
        for item in items:
            print(f"[{item.source}] {item.title} ({item.url})")
        return

    contributions = load_grok_contributions(config)
    if contributions:
        from integrations.grok_bot.contributions import filter_contributions_by_window

        contributions = filter_contributions_by_window(
            contributions, window_start, cutoff
        )
    curation_items = items
    event_overrides = {}
    if contributions:
        from integrations.grok_bot.contributions import (
            event_overrides as grok_event_overrides,
            extend_curation_items,
        )

        curation_items = extend_curation_items(items, contributions)
        event_overrides = grok_event_overrides(contributions)

    curation_result = {"candidates": [], "brief": {}}
    generation_error = None
    if curation_items:
        logger.info("=== Variable-count curation ===")
        try:
            curation_result = curate_daily_brief(curation_items, config.get("llm", {}))
        except CurationError as exc:
            generation_error = str(exc)
            logger.error(f"Curation failed: {exc}")
    events = (
        assemble_curated_events(curation_result, event_overrides)
        if not generation_error
        else []
    )
    if events:
        events = await verify_event_primary_sources(events)
    editions_dir = Path(config.get("output", {}).get("editions", {}).get("directory", "editions"))
    source_records = [result.to_record() for result in source_results]
    edition = build_edition(
        edition_id=edition_id,
        cutoff_at=cutoff.isoformat(),
        sources=source_records,
        events=events,
        attempt=next_attempt(editions_dir, edition_id) if args.dry_run else 1,
        contribution_count=len(contributions),
        generation_error=generation_error,
    )
    logger.info(f"Edition status: {edition['status']} events={len(edition['events'])}")

    if args.dry_run:
        print(json.dumps(edition, ensure_ascii=False, indent=2))
        return

    if edition["status"] != "published_candidate":
        logger.info("=== No edition directory written ===")
    else:
        lock_path = acquire_edition_lock(editions_dir, edition_id)
        try:
            # Read the retry counter only after winning the same-date mutex.
            edition = build_edition(
                edition_id=edition_id,
                cutoff_at=cutoff.isoformat(),
                sources=source_records,
                events=events,
                attempt=next_attempt(editions_dir, edition_id),
                contribution_count=len(contributions),
                generation_error=generation_error,
            )
            edition_path = write_edition(editions_dir, edition)
            html_path = write_web_edition(editions_dir, edition)
            logger.info(f"Wrote {edition_path} and {html_path}")
        finally:
            release_edition_lock(lock_path)

    # Legacy Markdown path remains available during migration.
    output_cfg = config.get("output", {})
    selected_count = await write_legacy_outputs(curation_result, output_cfg)
    logger.info(f"=== Done! edition events={len(edition['events'])} legacy items={selected_count} ===")


def main():
    parser = argparse.ArgumentParser(description="AI Daily Brief")
    parser.add_argument("--config", default="config.yaml", help="Config file path")
    parser.add_argument("--sources-only", action="store_true", help="Fetch sources only, no LLM")
    parser.add_argument("--dry-run", action="store_true", help="Print results, don't write files")
    args = parser.parse_args()

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
