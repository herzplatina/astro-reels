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
BUILD_DIR = OUTPUT_DIR / ".build"
CONFIG_PATH = ROOT / "config.json"

# Intermediate frames are deleted when a render succeeds and deliberately kept
# when it fails, so you can open them and see what tripped. Nothing removed the
# ones from a failed run, so they accumulated at ~300 KB a time.
BUILD_ARTIFACT_HOURS = 24


def prune(
    days: int, keep_minimum: int, dry_run: bool = False
) -> tuple[list[Path], float]:
    """Delete reels older than `days`, always keeping the newest `keep_minimum`.

    Returns the files removed and the megabytes reclaimed.
    """
    if not OUTPUT_DIR.is_dir():
        return [], 0.0

    # Read each mtime once, tolerating a file that disappears between the glob
    # and the stat — a concurrent render or a manual delete should not abort the
    # whole prune.
    dated = []
    for reel in OUTPUT_DIR.glob("*.mp4"):
        try:
            dated.append((reel.stat().st_mtime, reel))
        except FileNotFoundError:
            continue
    reels = [reel for _, reel in sorted(dated, key=lambda pair: pair[0], reverse=True)]
    # The newest N are exempt regardless of age, so a quiet month never leaves
    # you with nothing to look back at.
    candidates = reels[keep_minimum:]

    cutoff = time.time() - days * 86400
    removed: list[Path] = []
    freed = 0.0

    for reel in candidates:
        try:
            stats = reel.stat()
        except FileNotFoundError:
            continue
        if stats.st_mtime >= cutoff:
            continue

        caption = reel.with_suffix(".caption.txt")
        freed += stats.st_size
        if caption.exists():
            freed += caption.stat().st_size
        if not dry_run:
            reel.unlink(missing_ok=True)
            caption.unlink(missing_ok=True)
        removed.append(reel)

    return removed, freed / 1_048_576


def prune_build_artifacts(dry_run: bool = False) -> tuple[int, float]:
    """Delete intermediate frames left behind by failed renders."""
    if not BUILD_DIR.is_dir():
        return 0, 0.0

    cutoff = time.time() - BUILD_ARTIFACT_HOURS * 3600
    count = 0
    freed = 0.0
    for artifact in BUILD_DIR.glob("*.png"):
        try:
            stats = artifact.stat()
        except FileNotFoundError:
            continue
        if stats.st_mtime >= cutoff:
            continue
        freed += stats.st_size
        if not dry_run:
            artifact.unlink(missing_ok=True)
        count += 1
    return count, freed / 1_048_576


def prune_from_config(dry_run: bool = False) -> tuple[list[Path], float]:
    cfg = json.loads(CONFIG_PATH.read_text())
    retention = cfg.get("retention", {})
    if not retention.get("enabled", True):
        return [], 0.0
    removed, freed = prune(
        days=retention.get("days", 60),
        keep_minimum=retention.get("keep_minimum", 10),
        dry_run=dry_run,
    )
    _, build_freed = prune_build_artifacts(dry_run)
    return removed, freed + build_freed


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
