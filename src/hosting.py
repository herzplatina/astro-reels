#!/usr/bin/env python3
"""Host finished reels on GitHub Pages so Instagram can fetch them.

Instagram is the only platform that needs this. It never accepts a file upload —
it fetches the video from a public HTTPS URL — whereas YouTube takes a direct
resumable upload and TikTok takes a direct chunked upload.

Why Pages and not the obvious alternatives, all three tested:

    raw.githubusercontent.com   content-type: application/octet-stream
    release assets              content-type: application/octet-stream,
                                content-disposition: attachment, and a signed
                                URL that expires after an hour
    GitHub Pages                content-type: video/mp4, accept-ranges: bytes

GitHub only maps MIME types correctly for images; everything else falls back to
octet-stream, and `x-content-type-options: nosniff` stops a fetcher recovering
from it. Pages is a real static server, so it gets this right.

The branch is an orphan, force-pushed as a single amended commit. That matters:
committing videos normally would leave them in git history permanently, which
both bloats the repo forever and quietly contradicts the retention policy.

    python3 src/hosting.py --publish output/reel.mp4
    python3 src/hosting.py --list
    python3 src/hosting.py --unpublish reel.mp4
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLONE = ROOT / ".hosting"
BRANCH = "gh-pages"
SUBDIR = "reels"

# Pages caches at the edge for ten minutes, so a freshly pushed file can 404 for
# a short while and a deleted one can linger. Publishing polls; deletion does not
# need to.
AVAILABILITY_TIMEOUT_S = 300
POLL_INTERVAL_S = 5


class HostingError(RuntimeError):
    pass


def _git(*args: str, cwd: Path | None = None) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd or CLONE, capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise HostingError(f"git {' '.join(args)} failed:\n{proc.stderr.strip()}")
    return proc.stdout.strip()


def remote_url() -> str:
    """The push URL, taken from the main repo so credentials are reused."""
    return _git("remote", "get-url", "origin", cwd=ROOT)


def pages_base_url() -> str:
    """Derive https://<user>.github.io/<repo>/ from the git remote."""
    url = remote_url()
    trimmed = url.removesuffix(".git")
    if trimmed.startswith("git@"):
        _, _, path = trimmed.partition(":")
    else:
        path = "/".join(trimmed.split("/")[-2:])
    owner, _, repo = path.partition("/")
    if not owner or not repo:
        raise HostingError(f"Could not parse owner/repo from remote {url!r}")
    return f"https://{owner.lower()}.github.io/{repo}/"


def public_url(filename: str) -> str:
    return f"{pages_base_url()}{SUBDIR}/{filename}"


def ensure_clone() -> None:
    """Make sure a local checkout of the hosting branch exists and is current."""
    if (CLONE / ".git").is_dir():
        _git("fetch", "--depth", "1", "origin", BRANCH)
        _git("reset", "--hard", f"origin/{BRANCH}")
        _git("clean", "-fdq")
        return

    CLONE.parent.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(CLONE, ignore_errors=True)
    proc = subprocess.run(
        [
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            BRANCH,
            "--single-branch",
            remote_url(),
            str(CLONE),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise HostingError(
            f"Could not clone the {BRANCH} branch:\n{proc.stderr.strip()}\n"
            f"Create it first, or check that Pages is enabled on the repo."
        )


def _commit_and_push(message: str) -> None:
    _git("add", "-A")
    if not _git("status", "--porcelain"):
        return  # nothing changed
    # Amend onto a single root commit so the branch never accumulates history.
    _git(
        "-c",
        "user.name=astro-reels",
        "-c",
        "user.email=noreply@localhost",
        "commit",
        "--quiet",
        "--amend",
        "-m",
        message,
    )
    _git("push", "--force", "--quiet", "origin", BRANCH)


def wait_until_live(url: str, timeout: int = AVAILABILITY_TIMEOUT_S) -> bool:
    """Poll until Pages actually serves the file.

    Handing Instagram a URL that 404s wastes the publish attempt, and a Pages
    build takes appreciably longer than the push that triggers it.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            request = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(request, timeout=15) as response:
                if response.status == 200:
                    return True
        except Exception:  # noqa: BLE001 - a 404 while building is expected
            pass
        time.sleep(POLL_INTERVAL_S)
    return False


def publish(video: Path, wait: bool = True) -> str:
    """Put one video on Pages and return its public URL."""
    if not video.is_file():
        raise HostingError(f"No such file: {video}")

    ensure_clone()
    target_dir = CLONE / SUBDIR
    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(video, target_dir / video.name)
    _commit_and_push(f"host {video.name}")

    url = public_url(video.name)
    if wait and not wait_until_live(url):
        raise HostingError(
            f"{url} did not become available within {AVAILABILITY_TIMEOUT_S}s.\n"
            "Check that GitHub Pages is enabled and building from gh-pages."
        )
    return url


def unpublish(filename: str) -> bool:
    """Remove one video from Pages. Returns whether anything was removed."""
    ensure_clone()
    target = CLONE / SUBDIR / filename
    if not target.exists():
        return False
    target.unlink()
    _commit_and_push(f"unhost {filename}")
    return True


def list_hosted() -> list[str]:
    ensure_clone()
    directory = CLONE / SUBDIR
    if not directory.is_dir():
        return []
    return sorted(p.name for p in directory.iterdir() if p.is_file())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--publish", type=Path, metavar="VIDEO")
    group.add_argument("--unpublish", metavar="FILENAME")
    group.add_argument("--list", action="store_true")
    parser.add_argument("--no-wait", action="store_true")
    args = parser.parse_args()

    if args.list:
        hosted = list_hosted()
        print(f"{len(hosted)} hosted at {pages_base_url()}{SUBDIR}/")
        for name in hosted:
            print(f"  {name}")
        return 0

    if args.publish:
        url = publish(args.publish, wait=not args.no_wait)
        print(url)
        return 0

    removed = unpublish(args.unpublish)
    print("removed" if removed else "not hosted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
