#!/usr/bin/env python3
"""Build the background-music library from CC0-licensed sources.

Only CC0 is accepted: no attribution burden, commercial use permitted, and the
lowest risk of an Instagram/TikTok Content ID claim muting the reel.

Tracks are normalised to a consistent loudness so no reel is jarringly louder
than the last. Run again at any time to top up the library; existing files are
left alone.
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path

from audit_music import screen

ROOT = Path(__file__).resolve().parent.parent
MUSIC_DIR = ROOT / "assets" / "music"
MANIFEST = MUSIC_DIR / "manifest.json"

OPENVERSE = "https://api.openverse.org/v1/audio/"
# Openverse and the Freesound CDN both reject the default Python urllib agent.
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) divine-guidance/1.0"

# Categories chosen for the channel. Each query is run separately so the library
# stays balanced instead of being dominated by whichever term matches most.
QUERIES: dict[str, list[str]] = {
    "ambient": [
        "meditation ambient pad",
        "singing bowl",
        "ambient drone calm",
        "soft piano ambient",
    ],
    "nature": [
        "forest birds ambience",
        "gentle rain ambience",
        "ocean waves calm",
        "stream water flowing",
    ],
    "devotional": [
        "sitar",
        "tanpura drone",
        "bansuri flute",
        "tabla meditation",
    ],
}

MIN_DURATION_S = 8
MAX_DURATION_S = 600
PER_QUERY = 6
TARGET_LUFS = -20  # quiet: this sits under text, it is not the main event


@dataclass
class Track:
    id: str
    title: str
    category: str
    source: str
    source_url: str
    license: str
    filename: str


def search(query: str) -> list[dict]:
    params = urllib.parse.urlencode(
        {"q": query, "license": "cc0", "page_size": 20, "peaks": "false"}
    )
    req = urllib.request.Request(
        f"{OPENVERSE}?{params}",
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.load(resp)
    except Exception as exc:  # noqa: BLE001 - surface and continue to next query
        print(f"  ! search failed for {query!r}: {exc}", file=sys.stderr)
        return []

    out = []
    for item in payload.get("results", []):
        duration_ms = item.get("duration") or 0
        if not (MIN_DURATION_S * 1000 <= duration_ms <= MAX_DURATION_S * 1000):
            continue
        if not item.get("url"):
            continue

        # The background bed must be pure instrumental, always. Screen before
        # downloading so a vocal track never enters the library in the first
        # place; audit_music.py is the safety net for anything already there.
        tags = [t.get("name", "") for t in (item.get("tags") or [])]
        is_vocal, reasons = screen(
            item.get("title") or "", tags, item.get("genres") or []
        )
        if is_vocal:
            print(
                f"  - skipped {(item.get('title') or '')[:40]!r} "
                f"(voice: {', '.join(reasons)})"
            )
            continue

        out.append(item)
        if len(out) >= PER_QUERY:
            break
    return out


def download_and_normalise(item: dict, category: str) -> Track | None:
    track_id = item["id"]
    dest = MUSIC_DIR / category / f"{track_id}.mp3"
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists():
        return None  # already have it

    tmp = dest.with_suffix(".tmp")
    req = urllib.request.Request(item["url"], headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, tmp.open("wb") as fh:
            fh.write(resp.read())
    except Exception as exc:  # noqa: BLE001
        print(f"  ! download failed {item.get('title')!r}: {exc}", file=sys.stderr)
        tmp.unlink(missing_ok=True)
        return None

    # Normalise loudness and force a consistent mono-safe stereo 44.1k mp3.
    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(tmp),
            "-af",
            f"loudnorm=I={TARGET_LUFS}:TP=-2:LRA=11",
            "-ar",
            "44100",
            "-ac",
            "2",
            "-b:a",
            "160k",
            str(dest),
        ],
        capture_output=True,
        text=True,
    )
    tmp.unlink(missing_ok=True)
    if proc.returncode != 0:
        print(
            f"  ! transcode failed {item.get('title')!r}: {proc.stderr[:200]}",
            file=sys.stderr,
        )
        dest.unlink(missing_ok=True)
        return None

    return Track(
        id=track_id,
        title=(item.get("title") or "untitled").strip(),
        category=category,
        source=item.get("provider") or "unknown",
        source_url=item.get("foreign_landing_url") or "",
        license=item.get("license", "cc0"),
        filename=str(dest.relative_to(ROOT)),
    )


def main() -> int:
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    existing: list[dict] = []
    if MANIFEST.exists():
        existing = json.loads(MANIFEST.read_text())
    known = {t["id"] for t in existing}

    added: list[Track] = []
    for category, queries in QUERIES.items():
        print(f"\n{category}:")
        for query in queries:
            for item in search(query):
                if item["id"] in known:
                    continue
                track = download_and_normalise(item, category)
                if track:
                    added.append(track)
                    known.add(track.id)
                    print(f"  + {track.title[:50]}")

    manifest = existing + [asdict(t) for t in added]
    MANIFEST.write_text(json.dumps(manifest, indent=2))

    by_cat: dict[str, int] = {}
    for t in manifest:
        by_cat[t["category"]] = by_cat.get(t["category"], 0) + 1
    print(
        f"\nLibrary: {len(manifest)} tracks — "
        + ", ".join(f"{k} {v}" for k, v in sorted(by_cat.items()))
    )
    print(f"Added this run: {len(added)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
