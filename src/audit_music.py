#!/usr/bin/env python3
"""Screen the music library for anything with a human voice in it.

The channel's rule is absolute: background music must be pure instrumental,
always. A reel that starts singing over the text is worse than no reel.

Automatic detection of singing from raw audio is not reliable without a trained
model, so this screens the human-authored metadata instead — Freesound
contributors tag voice content thoroughly, and Openverse exposes those tags.
Anything matching is moved out of the library rather than deleted, so a false
positive can be walked back.

    python3 src/audit_music.py --dry-run
    python3 src/audit_music.py
    python3 src/audit_music.py --restore <track-id>
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MUSIC_DIR = ROOT / "assets" / "music"
MANIFEST = MUSIC_DIR / "manifest.json"
# Deliberately outside assets/music/ — pick_track scans that tree recursively,
# so a quarantine folder inside it would still be in the rotation.
QUARANTINE = ROOT / "assets" / "music_quarantine"
CACHE = ROOT / "assets" / "music" / ".metadata_cache.json"

OPENVERSE_DETAIL = "https://api.openverse.org/v1/audio/{}/"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) astro-reels/1.0"

# Unambiguous: if any of these appear in a title, tag or genre, there is a human
# voice in the recording.
VOCAL_TERMS = frozenset(
    {
        "vocal",
        "vocals",
        "voice",
        "voices",
        "vox",
        "choir",
        "chorus",
        "chant",
        "chanting",
        "chanted",
        "acapella",
        "a-cappella",
        "lyric",
        "lyrics",
        "speech",
        "speaking",
        "spoken",
        "talking",
        "talk",
        "conversation",
        "crowd",
        "people",
        "singer",
        "opera",
        "soprano",
        "tenor",
        "baritone",
        "humming",
        "whisper",
        "whispering",
        "scream",
        "shout",
        "narration",
        "podcast",
        "word",
        "words",
        "mantra",
        "kirtan",
        "bhajan",
        "gospel",
        "rap",
        "verse",
        # A human voice breaks the rule even when nobody is singing.
        "chat",
        "chatter",
        "murmur",
        "laugh",
        "laughter",
        "applause",
        "cheer",
        "cheering",
        "dialogue",
        "announcer",
        "interview",
        "babble",
        "yelling",
        "shouting",
        "child",
        "children",
        "baby",
    }
)

# Ambiguous on their own. "Singing bowl" is an instrument, and plenty of purely
# instrumental pieces are tagged "song". These only count when nothing in the
# metadata explains them away.
AMBIGUOUS_TERMS = frozenset({"singing", "sing", "sung", "song", "male", "female"})

# If any of these are present, an ambiguous term is explained and does not count.
INSTRUMENT_CONTEXT = frozenset(
    {
        "bowl",
        "bowls",
        "singing bowl",
        "tibetan",
        "birdsong",
        "bird",
        "birds",
        "songbird",
        "instrumental",
        "piano",
        "guitar",
        "harp",
        "sitar",
        "flute",
    }
)


def _fetch_metadata(track_id: str, cache: dict) -> dict | None:
    if track_id in cache:
        return cache[track_id]

    request = urllib.request.Request(
        OPENVERSE_DETAIL.format(track_id),
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except Exception as exc:  # noqa: BLE001 - a lookup failure is not fatal
        print(f"  ! lookup failed for {track_id}: {exc}", file=sys.stderr)
        return None

    cache[track_id] = payload
    time.sleep(0.2)  # be polite to a free public API
    return payload


def screen(title: str, tags: list[str], genres: list[str]) -> tuple[bool, list[str]]:
    """Decide whether a track contains a human voice.

    Returns (is_vocal, matched_terms).
    """
    raw = [t for t in tags + genres]
    raw += title.replace("_", " ").replace("-", " ").replace(".", " ").split()

    # Normalise hard, because the misses are all near-misses: a tag reading
    # "Peoples" rather than "people", or a filename token like "song23".
    haystack: set[str] = set()
    for item in raw:
        token = "".join(ch for ch in item.lower() if ch.isalpha())
        if not token:
            continue
        haystack.add(token)
        if token.endswith("s") and len(token) > 3:
            haystack.add(token[:-1])  # peoples -> people, chats -> chat
        if token.endswith("ing") and len(token) > 5:
            haystack.add(token[:-3])  # talking -> talk

    blob = " ".join(haystack)

    hard = sorted(haystack & VOCAL_TERMS)
    if hard:
        return True, hard

    soft = sorted(haystack & AMBIGUOUS_TERMS)
    if soft and not (haystack & INSTRUMENT_CONTEXT) and "bowl" not in blob:
        return True, [f"{term} (ambiguous)" for term in soft]

    return False, []


def quarantine(track: dict, reasons: list[str], dry_run: bool) -> bool:
    source = ROOT / track["filename"]
    if not source.exists():
        return False

    destination = QUARANTINE / track["category"] / source.name
    if not dry_run:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        (destination.with_suffix(".reason.txt")).write_text(
            f"{track['title']}\nmatched: {', '.join(reasons)}\n{track['source_url']}\n"
        )
    return True


def build_preview(seconds: float = 3.0) -> Path:
    """Stitch a few seconds of every track into one file, with a printed index.

    Tag screening is only as good as whoever wrote the tags. This is the check
    that does not depend on metadata at all: listen once, top to bottom, and
    anything with a voice in it is obvious immediately.
    """
    tracks = sorted(MUSIC_DIR.rglob("*.mp3"))
    if not tracks:
        raise SystemExit("No tracks to preview.")

    scratch = QUARANTINE.parent / ".preview_clips"
    scratch.mkdir(parents=True, exist_ok=True)
    listing = scratch / "clips.txt"
    manifest = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else []
    titles = {Path(e["filename"]).stem: e["title"] for e in manifest}

    lines, index, position = [], [], 0.0
    for number, track in enumerate(tracks):
        clip = scratch / f"{number:03d}.mp3"
        # Sample from a quarter of the way in — the opening of a track is often
        # a fade or silence, which would tell you nothing.
        offset = max(0.0, _probe_seconds(track) * 0.25)
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-ss",
                f"{offset:.2f}",
                "-t",
                f"{seconds}",
                "-i",
                str(track),
                "-af",
                "afade=in:st=0:d=0.2,afade=out:st=%.2f:d=0.2" % (seconds - 0.2),
                "-ar",
                "44100",
                "-ac",
                "2",
                str(clip),
            ],
            check=True,
            capture_output=True,
        )
        lines.append(f"file '{clip.name}'")
        index.append((position, track.parent.name, titles.get(track.stem, track.stem)))
        position += seconds

    listing.write_text("\n".join(lines))
    out = ROOT / "output" / "music_preview.mp3"
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(listing),
            "-c:a",
            "libmp3lame",
            "-b:a",
            "160k",
            str(out),
        ],
        check=True,
        capture_output=True,
    )
    shutil.rmtree(scratch, ignore_errors=True)

    print(f"{len(tracks)} tracks, {seconds}s each -> {out}\n")
    for start, category, title in index:
        print(f"  {int(start) // 60}:{int(start) % 60:02d}  {category:11} {title[:50]}")
    return out


def _probe_seconds(path: Path) -> float:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--restore", metavar="TRACK_ID", help="Undo one quarantine.")
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Build one audio montage of the whole library, to verify by ear.",
    )
    args = parser.parse_args()

    if args.preview:
        build_preview()
        return 0

    manifest = json.loads(MANIFEST.read_text())

    if args.restore:
        for entry in manifest:
            if entry["id"] != args.restore:
                continue
            held = QUARANTINE / entry["category"] / Path(entry["filename"]).name
            if not held.exists():
                raise SystemExit(f"{args.restore} is not in quarantine.")
            target = ROOT / entry["filename"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(held), str(target))
            held.with_suffix(".reason.txt").unlink(missing_ok=True)
            print(f"Restored {entry['title']}")
            return 0
        raise SystemExit(f"No track with id {args.restore}.")

    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    flagged: list[tuple[dict, list[str]]] = []
    unknown: list[dict] = []

    print(f"Screening {len(manifest)} tracks against Openverse metadata...\n")
    for track in manifest:
        metadata = _fetch_metadata(track["id"], cache)
        if metadata is None:
            unknown.append(track)
            continue

        tags = [t.get("name", "") for t in (metadata.get("tags") or [])]
        genres = metadata.get("genres") or []
        is_vocal, reasons = screen(track["title"], tags, genres)
        if is_vocal:
            flagged.append((track, reasons))

    CACHE.write_text(json.dumps(cache))

    moved = 0
    for track, reasons in flagged:
        if quarantine(track, reasons, args.dry_run):
            moved += 1
        verb = "would quarantine" if args.dry_run else "quarantined"
        print(f"  {verb}: {track['title'][:48]}")
        print(f"    matched: {', '.join(reasons)}")

    if unknown:
        print(f"\n  {len(unknown)} tracks could not be looked up — review by ear:")
        for track in unknown:
            print(f"    ? {track['title'][:60]}")

    if not args.dry_run and moved:
        kept = [t for t in manifest if t["id"] not in {f["id"] for f, _ in flagged}]
        MANIFEST.write_text(json.dumps(kept, indent=2))

    remaining = len(manifest) - (0 if args.dry_run else moved)
    print(f"\n{moved} flagged, {remaining} tracks remain instrumental-only.")
    if moved and not args.dry_run:
        print(f"Quarantined files kept at {QUARANTINE} — restore with --restore <id>.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
