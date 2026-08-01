#!/usr/bin/env python3
"""Tests for the GitHub Pages hosting mechanics, against a real local git repo.

A mutation audit showed `hosting.py` had no tests at all, and two of the
surviving mutants were dangerous: committing without `--amend` (so videos
accumulate in history forever) and pointing `BRANCH` at `main` (so videos land
in the source branch's permanent history). Both are exactly what the orphan
force-push design exists to prevent.

These use a bare repository on disk as the remote, so the git behaviour is
genuinely exercised without touching GitHub.

    python3 -m pytest tests/test_hosting.py -q
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import hosting  # noqa: E402

pytestmark = pytest.mark.allow_network  # local git only; no actual network


def git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def remote(tmp_path, monkeypatch):
    """A bare repo with a main branch and an orphan gh-pages branch."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git("init", "--bare", "--initial-branch=main", cwd=origin)

    seed = tmp_path / "seed"
    seed.mkdir()
    git("init", "--initial-branch=main", cwd=seed)
    git("config", "user.email", "t@t", cwd=seed)
    git("config", "user.name", "t", cwd=seed)
    (seed / "README.md").write_text("source branch\n")
    git("add", "-A", cwd=seed)
    git("commit", "-m", "main content", cwd=seed)
    git("remote", "add", "origin", str(origin), cwd=seed)
    git("push", "-u", "origin", "main", cwd=seed)

    git("checkout", "--orphan", "gh-pages", cwd=seed)
    git("rm", "-rf", ".", cwd=seed)
    (seed / "index.html").write_text("hosting root\n")
    git("add", "-A", cwd=seed)
    git("commit", "-m", "hosting root", cwd=seed)
    git("push", "-u", "origin", "gh-pages", cwd=seed)

    monkeypatch.setattr(hosting, "CLONE", tmp_path / "clone")
    monkeypatch.setattr(hosting, "remote_url", lambda: str(origin))
    monkeypatch.setattr(
        hosting, "pages_base_url", lambda: "https://user.github.io/repo/"
    )
    return origin


def branch_files(origin, branch):
    out = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", branch],
        cwd=origin,
        capture_output=True,
        text=True,
    )
    return sorted(f for f in out.stdout.split() if f)


def branch_depth(origin, branch):
    return int(git("rev-list", "--count", branch, cwd=origin))


# ------------------------------------------------------------------ publish


def test_a_video_reaches_the_hosting_branch(remote, tmp_path):
    video = tmp_path / "reel.mp4"
    video.write_bytes(b"video bytes")

    url = hosting.push(video)

    assert url.endswith("/reels/reel.mp4")
    assert "reels/reel.mp4" in branch_files(remote, "gh-pages")


def test_videos_never_reach_the_source_branch(remote, tmp_path):
    """The mutant that made BRANCH 'main' would commit videos into the source
    branch's permanent history — the exact outcome the design prevents."""
    video = tmp_path / "reel.mp4"
    video.write_bytes(b"video bytes")
    hosting.push(video)

    assert hosting.BRANCH == "gh-pages"
    main_files = branch_files(remote, "main")
    assert not any(f.endswith(".mp4") for f in main_files), main_files
    assert branch_depth(remote, "main") == 1, "main history must be untouched"


def test_hosting_history_never_grows(remote, tmp_path):
    """Committing without --amend accumulates every video forever, and git
    history cannot be pruned the way output/ can."""
    for index in range(4):
        video = tmp_path / f"reel{index}.mp4"
        video.write_bytes(b"x" * 32)
        hosting.push(video)
        assert branch_depth(remote, "gh-pages") == 1

    hosting.unpublish("reel0.mp4")
    assert branch_depth(remote, "gh-pages") == 1


def test_several_videos_can_be_hosted_at_once(remote, tmp_path):
    """Amending must not drop what is already hosted — a reel awaiting a retry
    has to survive the next reel being published."""
    for name in ("one.mp4", "two.mp4"):
        video = tmp_path / name
        video.write_bytes(b"x")
        hosting.push(video)

    assert sorted(hosting.list_hosted()) == ["one.mp4", "two.mp4"]


def test_unpublish_removes_only_its_own_file(remote, tmp_path):
    for name in ("keep.mp4", "drop.mp4"):
        video = tmp_path / name
        video.write_bytes(b"x")
        hosting.push(video)

    assert hosting.unpublish("drop.mp4") is True
    assert hosting.list_hosted() == ["keep.mp4"]
    assert "reels/drop.mp4" not in branch_files(remote, "gh-pages")


def test_unpublishing_something_absent_is_not_an_error(remote):
    assert hosting.unpublish("never-existed.mp4") is False


def test_the_hosting_root_survives_publishing(remote, tmp_path):
    video = tmp_path / "reel.mp4"
    video.write_bytes(b"x")
    hosting.push(video)
    assert "index.html" in branch_files(remote, "gh-pages")


# ------------------------------------------------------------------ guards


def test_only_mp4_may_be_published(remote, tmp_path):
    """push() copies a file to a public site and force-pushes it."""
    secret = tmp_path / "credentials.json"
    secret.write_text('{"token": "very secret"}')

    with pytest.raises(hosting.HostingError) as caught:
        hosting.push(secret)
    assert "only .mp4" in str(caught.value)
    assert not any(f.endswith(".json") for f in branch_files(remote, "gh-pages"))


def test_pushing_a_missing_file_fails_clearly(remote, tmp_path):
    with pytest.raises(hosting.HostingError):
        hosting.push(tmp_path / "absent.mp4")


def test_unpublish_cannot_escape_the_hosted_directory(remote, tmp_path):
    video = tmp_path / "reel.mp4"
    video.write_bytes(b"x")
    hosting.push(video)

    # "../" lands on the hosting root, which exists; "../../" escapes the clone
    # entirely and would be a no-op either way, proving nothing.
    assert (hosting.CLONE / hosting.SUBDIR / "../index.html").exists()

    assert hosting.unpublish("../index.html") is False
    assert "index.html" in branch_files(remote, "gh-pages")
    assert "reels/reel.mp4" in branch_files(remote, "gh-pages")


# ------------------------------------------------------------- URL building


@pytest.mark.parametrize(
    "remote_url,expected",
    [
        (
            "https://github.com/Herzplatina/astro-reels.git",
            "https://herzplatina.github.io/astro-reels/",
        ),
        (
            "https://github.com/herzplatina/astro-reels",
            "https://herzplatina.github.io/astro-reels/",
        ),
        (
            "git@github.com:herzplatina/astro-reels.git",
            "https://herzplatina.github.io/astro-reels/",
        ),
    ],
)
def test_pages_url_is_derived_from_the_remote(monkeypatch, remote_url, expected):
    monkeypatch.setattr(hosting, "remote_url", lambda: remote_url)
    assert hosting.pages_base_url() == expected


def test_an_unparseable_remote_is_reported(monkeypatch):
    monkeypatch.setattr(hosting, "remote_url", lambda: "not-a-remote")
    with pytest.raises(hosting.HostingError):
        hosting.pages_base_url()


def test_public_url_places_files_under_the_reels_path(monkeypatch):
    monkeypatch.setattr(hosting, "remote_url", lambda: "https://github.com/o/r.git")
    assert hosting.public_url("x.mp4") == "https://o.github.io/r/reels/x.mp4"
