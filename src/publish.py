#!/usr/bin/env python3
"""Publish one reel to all three platforms, then clean up the host.

The ordering rule that governs everything here: a video is removed from GitHub
Pages **only** once all three platforms have confirmed publication. A partial
success leaves the file hosted so the failed platforms can be retried against
the same URL.

    python3 src/publish.py output/reel.mp4 --dry-run   # rehearse, no API calls
    python3 src/publish.py output/reel.mp4             # host, publish, clean up
    python3 src/publish.py --status                    # what is in flight
    python3 src/publish.py --retry <slug>              # retry failed platforms
    python3 src/publish.py --sweep                     # release stranded hosts

State lives in output/publish_state.json so an interrupted run resumes rather
than double-posting.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

import hosting
from platforms import (
    PLATFORMS,
    CredentialsMissing,
    PublishResult,
    publish_instagram,
    publish_tiktok,
    publish_youtube,
)

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "output" / "publish_state.json"

# A host that never got cleaned up because a publish failed should not sit
# public forever. This is only a backstop; the normal path is publish-gated.
STRANDED_AFTER_DAYS = 14


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------- state


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except json.JSONDecodeError:
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def entry_for(state: dict, video: Path) -> dict:
    slug = video.stem
    if slug not in state:
        state[slug] = {
            "file": str(video.relative_to(ROOT)) if video.is_absolute() else str(video),
            "hosted_url": "",
            "hosted_at": "",
            "platforms": {p: {"status": "pending"} for p in PLATFORMS},
            "cleaned_up": False,
        }
    return state[slug]


def all_published(entry: dict) -> bool:
    return all(
        entry["platforms"].get(p, {}).get("status") == "published" for p in PLATFORMS
    )


def pending_platforms(entry: dict) -> list[str]:
    return [
        p
        for p in PLATFORMS
        if entry["platforms"].get(p, {}).get("status") != "published"
    ]


# ---------------------------------------------------------------- captions


def read_caption(video: Path, platform: str) -> str:
    """Pull one platform's section out of the caption file beside the video."""
    caption_file = video.with_suffix(".caption.txt")
    if not caption_file.exists():
        return video.stem.replace("-", " ")

    marker = f"--- {platform.upper()} ---"
    text = caption_file.read_text()
    if marker not in text:
        return text.strip()
    section = text.split(marker, 1)[1]
    for other in PLATFORMS:
        section = section.split(f"--- {other.upper()} ---", 1)[0]
    return section.strip()


# --------------------------------------------------------------- publishing


def publish_one(platform: str, video: Path, url: str, dry_run: bool) -> PublishResult:
    caption = read_caption(video, platform)

    if dry_run:
        return PublishResult(True, post_id=f"dry-run-{platform}")

    try:
        if platform == "instagram":
            return publish_instagram(url, caption)
        if platform == "youtube":
            title = caption.split("\n")[0][:100]
            return publish_youtube(video, title, caption)
        if platform == "tiktok":
            return publish_tiktok(video, caption)
    except CredentialsMissing as exc:
        return PublishResult(False, error=str(exc))
    except Exception as exc:  # noqa: BLE001 - one platform must not stop the rest
        return PublishResult(False, error=f"{type(exc).__name__}: {exc}")

    return PublishResult(False, error=f"unknown platform {platform!r}")


def run(video: Path, dry_run: bool = False, only: list[str] | None = None) -> int:
    state = load_state()
    entry = entry_for(state, video)

    targets = only or pending_platforms(entry)
    if not targets:
        print(f"{video.name} is already published everywhere.")
        return 0

    # Host first. Instagram cannot publish without a public URL, and hosting
    # once for all retries keeps the URL stable.
    if not entry["hosted_url"]:
        if dry_run:
            entry["hosted_url"] = hosting.public_url(video.name)
            print(f"  would host at {entry['hosted_url']}")
        else:
            print(f"  hosting {video.name}...")
            entry["hosted_url"] = hosting.publish(video)
            print(f"  hosted at {entry['hosted_url']}")
        entry["hosted_at"] = _now()
        save_state(state)

    for platform in targets:
        result = publish_one(platform, video, entry["hosted_url"], dry_run)
        entry["platforms"][platform] = {
            "status": "published" if result.ok else "failed",
            "id": result.post_id,
            "error": result.error,
            "at": _now(),
        }
        mark = "ok" if result.ok else "FAILED"
        detail = result.post_id if result.ok else result.error
        print(f"  {platform:10} {mark:7} {detail}")
        save_state(state)

    # The rule: the host is released only when every platform has confirmed.
    if all_published(entry) and not entry["cleaned_up"]:
        if dry_run:
            print("  would remove from hosting (all three confirmed)")
        else:
            hosting.unpublish(video.name)
            print("  removed from hosting (all three confirmed)")
        entry["cleaned_up"] = True
    elif not all_published(entry):
        remaining = ", ".join(pending_platforms(entry))
        print(f"  kept hosted — not yet published on: {remaining}")

    save_state(state)
    return 0 if all_published(entry) else 1


# ------------------------------------------------------------------ reports


def show_status() -> int:
    state = load_state()
    if not state:
        print("Nothing published yet.")
        return 0

    for slug, entry in sorted(state.items()):
        flags = " ".join(
            f"{p[:2]}:{entry['platforms'].get(p, {}).get('status', '?')[:4]}"
            for p in PLATFORMS
        )
        host = "released" if entry["cleaned_up"] else "hosted"
        print(f"  {slug[:44]:46} {flags:28} {host}")
    return 0


def sweep(dry_run: bool = False) -> int:
    """Release hosts stranded by a publish that never completed."""
    state = load_state()
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=STRANDED_AFTER_DAYS)
    released = 0

    for slug, entry in state.items():
        if entry["cleaned_up"] or not entry["hosted_at"]:
            continue
        if dt.datetime.fromisoformat(entry["hosted_at"]) > cutoff:
            continue

        name = Path(entry["file"]).name
        remaining = ", ".join(pending_platforms(entry))
        print(
            f"  {'would release' if dry_run else 'released'} {name} "
            f"(stranded {STRANDED_AFTER_DAYS}d, never published on: {remaining})"
        )
        if not dry_run:
            hosting.unpublish(name)
            entry["cleaned_up"] = True
        released += 1

    if not dry_run:
        save_state(state)
    if not released:
        print(f"Nothing stranded beyond {STRANDED_AFTER_DAYS} days.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", nargs="?", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--sweep", action="store_true")
    parser.add_argument("--retry", metavar="SLUG")
    parser.add_argument(
        "--only", nargs="+", choices=PLATFORMS, help="Publish to these platforms only."
    )
    args = parser.parse_args()

    if args.status:
        return show_status()
    if args.sweep:
        return sweep(args.dry_run)

    if args.retry:
        state = load_state()
        entry = state.get(args.retry)
        if not entry:
            raise SystemExit(f"No record of {args.retry!r}. Try --status.")
        video = ROOT / entry["file"]
        if not video.exists():
            raise SystemExit(f"{video} is gone — re-render before retrying.")
        return run(video, args.dry_run, only=pending_platforms(entry))

    if not args.video:
        parser.error("Provide a video, or use --status / --sweep / --retry.")
    if not args.video.exists():
        raise SystemExit(f"No such file: {args.video}")

    return run(args.video, args.dry_run, only=args.only)


if __name__ == "__main__":
    raise SystemExit(main())
