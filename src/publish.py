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
import os
import sys
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

# After this many transient failures a platform stops being retried
# automatically. Without a ceiling a genuinely broken reel would be retried
# forever and the failure would never rise above the noise.
MAX_ATTEMPTS = 5

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
        # Losing this file means forgetting what was already published, which on
        # the next run means posting it all again. Keep the wreckage and say so
        # loudly rather than starting from scratch in silence.
        salvage = STATE_PATH.with_name(
            f"{STATE_PATH.stem}.corrupt-{dt.datetime.now().strftime('%Y%m%d%H%M%S')}.json"
        )
        STATE_PATH.rename(salvage)
        print(
            f"  ! publish state was unreadable and has been moved to {salvage.name}.\n"
            f"    Treating history as empty — check that file before publishing "
            f"anything, or a reel may be posted twice.",
            file=sys.stderr,
        )
        return {}


def save_state(state: dict) -> None:
    """Write atomically: a partial write here would erase the publish history."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, STATE_PATH)  # atomic on POSIX; never a half-written file


def entry_for(state: dict, video: Path) -> dict:
    slug = video.stem
    if slug not in state:
        state[slug] = {
            "file": _record_path(video),
            "hosted_url": "",
            "hosted_at": "",
            "platforms": {p: {"status": "pending"} for p in PLATFORMS},
            "cleaned_up": False,
        }

    # Fill in anything an older state file predates, so a schema change never
    # turns into a KeyError halfway through a publish.
    entry = state[slug]
    entry.setdefault("hosted_url", "")
    entry.setdefault("hosted_at", "")
    entry.setdefault("cleaned_up", False)
    entry.setdefault("platforms", {})
    for platform in PLATFORMS:
        entry["platforms"].setdefault(platform, {"status": "pending"})
    return entry


def _record_path(video: Path) -> str:
    """Store a repo-relative path when possible, an absolute one otherwise.

    `--out` accepts any destination, and relative_to() raises for anything
    outside the repo — which would crash before a single platform was tried.
    """
    if not video.is_absolute():
        return str(video)
    try:
        return str(video.relative_to(ROOT))
    except ValueError:
        return str(video)


def all_published(entry: dict) -> bool:
    platforms = entry.get("platforms", {})
    return all(platforms.get(p, {}).get("status") == "published" for p in PLATFORMS)


def pending_platforms(entry: dict) -> list[str]:
    """Everything not yet published, regardless of why."""
    platforms = entry.get("platforms", {})
    return [p for p in PLATFORMS if platforms.get(p, {}).get("status") != "published"]


def blocked_platforms(entry: dict) -> list[str]:
    """Platforms where retrying is pointless — refused outright, or out of attempts.

    Kept separate from `pending` so a permanent refusal stops consuming attempts
    and starts demanding a human instead.
    """
    blocked = []
    for platform in pending_platforms(entry):
        record = entry.get("platforms", {}).get(platform, {})
        if record.get("permanent") or record.get("attempts", 0) >= MAX_ATTEMPTS:
            blocked.append(platform)
    return blocked


def retryable_platforms(entry: dict) -> list[str]:
    blocked = set(blocked_platforms(entry))
    return [p for p in pending_platforms(entry) if p not in blocked]


def release_host(entry: dict, video: Path, dry_run: bool, reason: str) -> None:
    """Remove the video from hosting. Idempotent, and safe to call again.

    Reachable from every exit path on purpose: a crash between the last
    successful publish and this call would otherwise strand the file publicly
    with no route back, since a later run would find nothing left to publish.
    """
    if entry.get("cleaned_up"):
        return
    if dry_run:
        print(f"  would remove from hosting ({reason})")
        return
    hosting.unpublish(video.name)
    entry["cleaned_up"] = True
    print(f"  removed from hosting ({reason})")


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
            # YouTube rejects an empty title outright, and a caption whose first
            # line is blank would produce one.
            first_line = next(
                (line for line in caption.splitlines() if line.strip()), ""
            )
            title = (first_line or video.stem.replace("-", " "))[:100]
            return publish_youtube(video, title, caption)
        if platform == "tiktok":
            return publish_tiktok(video, caption)
    except CredentialsMissing as exc:
        # Nothing about retrying fixes an absent token — this needs a person.
        return PublishResult(False, error=str(exc), permanent=True)
    except Exception as exc:  # noqa: BLE001 - one platform must not stop the rest
        return PublishResult(False, error=f"{type(exc).__name__}: {exc}")

    return PublishResult(False, error=f"unknown platform {platform!r}")


def run(video: Path, dry_run: bool = False, only: list[str] | None = None) -> int:
    state = load_state()
    entry = entry_for(state, video)

    # An explicit --only must still never re-post somewhere that already
    # succeeded; the platform has no idea it is a repeat and would publish twice.
    outstanding = set(pending_platforms(entry))
    if only:
        already = [p for p in only if p not in outstanding]
        if already:
            print(f"  skipping {', '.join(already)} — already published")
        targets = [p for p in only if p in outstanding]
    else:
        targets = retryable_platforms(entry)

    # Nothing left to attempt. This branch must still reach the cleanup, because
    # a crash after the final publish but before the unpublish lands here on the
    # next run — and an early return would strand the file public forever.
    if not targets:
        if all_published(entry):
            release_host(entry, video, dry_run, "all three confirmed")
            save_state(state)
            print(f"{video.name} is published everywhere.")
            return 0

        blocked = blocked_platforms(entry)
        print(f"  no retries left for: {', '.join(blocked)}")
        for platform in blocked:
            record = entry["platforms"][platform]
            why = "refused" if record.get("permanent") else "out of attempts"
            print(f"    {platform}: {why} — {record.get('error', '')[:90]}")
        print(
            "  still hosted. Fix the cause and use --retry, or give up on this reel\n"
            f"  with --abandon {video.stem} to release the host."
        )
        save_state(state)
        return 1

    # Host first. Instagram cannot publish without a public URL, and hosting
    # once for all retries keeps the URL stable.
    if not entry["hosted_url"]:
        if dry_run:
            entry["hosted_url"] = hosting.public_url(video.name)
            entry["hosted_at"] = _now()
            print(f"  would host at {entry['hosted_url']}")
        else:
            print(f"  hosting {video.name}...")
            # Record the URL the moment the file is public, before waiting on
            # the Pages build. A timeout after this point leaves a file that
            # cleanup and --sweep can still find; recording afterwards would
            # orphan it with no record anywhere.
            entry["hosted_url"] = hosting.push(video)
            entry["hosted_at"] = _now()
            save_state(state)

            if not hosting.wait_until_live(entry["hosted_url"]):
                save_state(state)
                raise SystemExit(
                    f"  {entry['hosted_url']} did not start serving within "
                    f"{hosting.AVAILABILITY_TIMEOUT_S}s.\n"
                    f"  The file IS hosted and recorded — re-run to continue, or "
                    f"--abandon {video.stem} to release it."
                )
            print(f"  hosted at {entry['hosted_url']}")
        save_state(state)

    for platform in targets:
        previous = entry["platforms"].get(platform, {})
        result = publish_one(platform, video, entry["hosted_url"], dry_run)
        attempts = previous.get("attempts", 0) + 1
        entry["platforms"][platform] = {
            "status": "published" if result.ok else "failed",
            "id": result.post_id,
            "error": result.error,
            "attempts": attempts,
            "permanent": bool(result.permanent),
            "at": _now(),
        }

        if result.ok:
            print(f"  {platform:10} ok      {result.post_id}")
        else:
            note = (
                "will not retry"
                if result.permanent
                else f"attempt {attempts}/{MAX_ATTEMPTS}"
            )
            print(f"  {platform:10} FAILED  [{note}] {result.error}")
        save_state(state)

    # The rule: the host is released only when every platform has confirmed.
    if all_published(entry):
        release_host(entry, video, dry_run, "all three confirmed")
    else:
        remaining = ", ".join(pending_platforms(entry))
        print(f"  kept hosted — not yet published on: {remaining}")

    save_state(state)
    return 0 if all_published(entry) else 1


def abandon(slug: str, dry_run: bool = False) -> int:
    """Give up on a reel: release the host and stop tracking it as outstanding.

    The deliberate escape hatch from a permanently failing publish. Without it
    the only ways out would be waiting for the sweep or editing state by hand.
    """
    state = load_state()
    entry = state.get(slug)
    if not entry:
        raise SystemExit(f"No record of {slug!r}. Try --status.")

    published = [
        p
        for p in PLATFORMS
        if entry["platforms"].get(p, {}).get("status") == "published"
    ]
    if published:
        print(f"  note: already live on {', '.join(published)} — those posts stay up.")

    release_host(entry, Path(entry["file"]), dry_run, f"abandoned {slug}")
    if not dry_run:
        entry["abandoned"] = True
        save_state(state)
    return 0


# ------------------------------------------------------------------ reports


def show_status() -> int:
    state = load_state()
    if not state:
        print("Nothing published yet.")
        return 0

    needs_attention = []
    for slug, entry in sorted(state.items()):
        platforms = entry.get("platforms", {})
        flags = " ".join(
            f"{p[:2]}:{platforms.get(p, {}).get('status', '?')[:4]}" for p in PLATFORMS
        )
        if entry.get("abandoned"):
            host = "abandoned"
        elif entry.get("cleaned_up"):
            host = "released"
        else:
            host = "HOSTED"
        print(f"  {slug[:44]:46} {flags:28} {host}")
        if blocked_platforms(entry) and not entry.get("abandoned"):
            needs_attention.append(slug)

    # A stuck reel keeps a file public indefinitely, so it must not be something
    # you have to notice by reading the table carefully.
    if needs_attention:
        print(f"\n{len(needs_attention)} stuck and still hosted — needs a decision:")
        for slug in needs_attention:
            entry = state[slug]
            for platform in blocked_platforms(entry):
                record = entry["platforms"][platform]
                why = "refused" if record.get("permanent") else "out of attempts"
                print(
                    f"  {slug[:38]:40} {platform:10} {why}: {record.get('error', '')[:60]}"
                )
        print(
            "\n  --retry <slug> after fixing the cause, or --abandon <slug> to give up."
        )
    return 0


def sweep(dry_run: bool = False) -> int:
    """Release hosts stranded by a publish that never completed."""
    state = load_state()
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=STRANDED_AFTER_DAYS)
    released = 0

    for slug, entry in state.items():
        if entry.get("cleaned_up") or entry.get("abandoned"):
            continue
        hosted_at = entry.get("hosted_at")
        if not hosted_at:
            continue
        try:
            when = dt.datetime.fromisoformat(hosted_at)
        except ValueError:
            # An unparseable timestamp must not stop the sweep reaching the rest
            # of the list; treat it as old enough to deal with.
            print(f"  ! {slug}: unreadable hosted_at {hosted_at!r}, releasing anyway")
            when = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        if when > cutoff:
            continue

        name = Path(entry.get("file", slug)).name
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
        "--abandon",
        metavar="SLUG",
        help="Give up on a reel and release its hosted file.",
    )
    parser.add_argument(
        "--only", nargs="+", choices=PLATFORMS, help="Publish to these platforms only."
    )
    args = parser.parse_args()

    if args.status:
        return show_status()
    if args.sweep:
        return sweep(args.dry_run)
    if args.abandon:
        return abandon(args.abandon, args.dry_run)

    if args.retry:
        state = load_state()
        entry = state.get(args.retry)
        if not entry:
            raise SystemExit(f"No record of {args.retry!r}. Try --status.")
        video = ROOT / entry["file"]
        if not video.exists():
            raise SystemExit(f"{video} is gone — re-render before retrying.")

        # An explicit retry is a person saying they have fixed the cause, so it
        # clears the block that automatic runs respect. Otherwise repairing
        # credentials would leave the reel permanently unretryable.
        for platform in pending_platforms(entry):
            entry["platforms"][platform]["permanent"] = False
            entry["platforms"][platform]["attempts"] = 0
        save_state(state)

        return run(video, args.dry_run, only=pending_platforms(entry))

    if not args.video:
        parser.error("Provide a video, or use --status / --sweep / --retry.")
    if not args.video.exists():
        raise SystemExit(f"No such file: {args.video}")

    return run(args.video, args.dry_run, only=args.only)


if __name__ == "__main__":
    raise SystemExit(main())
