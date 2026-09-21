#!/usr/bin/env python3
"""Cloud Agent helper: merge same-event candidates, then Jev assist-only scoring.

Call this after gathering public sources and merging same-event reports, and
before source re-verification and final selection.

Scores are advisory. They never select, reject, or declare an empty edition.
On disable, missing key, quota, store failure, or API failure the original
candidate list continues (unscored rows may still be selected).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from generation.jev.assist import assist_candidates, load_candidates_payload  # noqa: E402
from generation.jev.prior import load_prior_coverage  # noqa: E402


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--print-ca-notes",
        action="store_true",
        help="Print day-刊 / CA template wording (fail-open, no kill-by-score) and exit",
    )
    parser.add_argument("--candidates", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--prior", type=Path, help="Optional JSON list of prior coverage rows")
    parser.add_argument("--editions", type=Path, default=Path("editions"))
    parser.add_argument("--store", type=Path, help="Override private Jev quota/reuse file")
    parser.add_argument("--exclude-edition", help="edition_id to omit from prior coverage")
    args = parser.parse_args(argv)
    if args.print_ca_notes:
        notes = ROOT / "generation" / "CA-JEV-ASSIST.md"
        sys.stdout.write(notes.read_text(encoding="utf-8"))
        return 0
    if args.candidates is None or args.out is None:
        parser.error("--candidates and --out are required unless --print-ca-notes")

    payload = _load_json(args.candidates)
    candidates = load_candidates_payload(payload)
    if args.prior:
        prior = _load_json(args.prior)
        if not isinstance(prior, list):
            raise SystemExit("--prior must be a JSON list")
    else:
        prior = load_prior_coverage(args.editions, exclude_edition_id=args.exclude_edition)

    config: dict = {
        "editions_dir": str(args.editions),
        "exclude_edition_id": args.exclude_edition,
        "repo_root": str(ROOT),
    }
    if args.store:
        config["store_path"] = str(args.store)

    result = assist_candidates(candidates, config=config, prior_coverage=prior)
    output = {
        "candidates": result.candidates,
        "jev_assist": result.summary,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result.summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
