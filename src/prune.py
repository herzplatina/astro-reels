#!/usr/bin/env python3
"""Age out old rendered reels so `output/` cannot grow without bound.

Runs automatically after every render, and standalone:

    python3 src/prune.py --dry-run
    python3 src/prune.py --days 30

A reel is only ever deleted from `output/` — the source text and the music
library are untouched, so anything pruned can be re-rendered byte-identically
by passing the same text and `--seed`.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "output"
CONFIG_PATH = ROOT / "config.json"


def prune(
    days: int, keep_minimum: int, dry_run: bool = False
) -> tuple[list[Path], float]:
    """Delete reels older than `days`, always keeping the newest `keep_minimum`.

    Returns the files removed and the megabytes reclaimed.
    """
    if not OUTPUT_DIR.is_dir():
        return [], 0.0

    reels = sorted(
        OUTPUT_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    # The newest N are exempt regardless of age, so a quiet month never leaves
    # you with nothing to look back at.
    candidates = reels[keep_minimum:]

    cutoff = time.time() - days * 86400
    removed: list[Path] = []
    freed = 0.0

    for reel in candidates:
        if reel.stat().st_mtime >= cutoff:
            continue
        caption = reel.with_suffix(".caption.txt")
        freed += reel.stat().st_size
        if caption.exists():
            freed += caption.stat().st_size
        if not dry_run:
            reel.unlink()
            caption.unlink(missing_ok=True)
        removed.append(reel)

    return removed, freed / 1_048_576


def prune_from_config(dry_run: bool = False) -> tuple[list[Path], float]:
    cfg = json.loads(CONFIG_PATH.read_text())
    retention = cfg.get("retention", {})
    if not retention.get("enabled", True):
        return [], 0.0
    return prune(
        days=retention.get("days", 60),
        keep_minimum=retention.get("keep_minimum", 10),
        dry_run=dry_run,
    )


def main() -> int:
    cfg = json.loads(CONFIG_PATH.read_text())
    retention = cfg.get("retention", {})

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=retention.get("days", 60))
    parser.add_argument(
        "--keep",
        type=int,
        default=retention.get("keep_minimum", 10),
        help="Always keep this many most-recent reels, whatever their age.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    removed, freed = prune(args.days, args.keep, args.dry_run)
    verb = "would remove" if args.dry_run else "removed"

    if not removed:
        print(
            f"Nothing to prune (older than {args.days} days, keeping newest {args.keep})."
        )
        return 0

    for reel in removed:
        print(f"  {verb}  {reel.name}")
    print(f"\n{verb} {len(removed)} reels — {freed:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
