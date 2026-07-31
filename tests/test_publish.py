#!/usr/bin/env python3
"""Tests for the publish-gated cleanup flow.

The rule under test throughout: a video leaves the host only when all three
platforms have confirmed. Everything else — retries, sweeps, resumes — exists to
serve that rule, so these tests drive it through the partial-failure paths that
cannot be exercised against the live APIs.

    python3 -m pytest tests/ -q
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import publish as publish_mod  # noqa: E402
from platforms import PublishResult  # noqa: E402


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """An isolated state file, a fake video, and a stubbed host."""
    monkeypatch.setattr(publish_mod, "STATE_PATH", tmp_path / "publish_state.json")
    monkeypatch.setattr(publish_mod, "ROOT", tmp_path)

    video = tmp_path / "a-reel.mp4"
    video.write_bytes(b"not really a video")
    video.with_suffix(".caption.txt").write_text(
        "--- INSTAGRAM ---\nthe words #tagone #tagtwo\n\n"
        "--- TIKTOK ---\nthe words #tt\n\n"
        "--- YOUTUBE ---\nthe words #Shorts\n"
    )

    hosted: dict[str, bool] = {}
    monkeypatch.setattr(
        publish_mod.hosting,
        "publish",
        lambda v, wait=True: (
            hosted.setdefault(v.name, True)
            and f"https://host/{v.name}"
            or f"https://host/{v.name}"
        ),
    )
    monkeypatch.setattr(
        publish_mod.hosting,
        "unpublish",
        lambda name: hosted.pop(name, None) is not None,
    )
    monkeypatch.setattr(
        publish_mod.hosting, "public_url", lambda name: f"https://host/{name}"
    )
    return video, hosted


def _results(monkeypatch, mapping: dict[str, bool]):
    """Force each platform to a fixed outcome."""

    def fake(platform, video, url, dry_run):
        ok = mapping[platform]
        return PublishResult(
            ok, post_id=f"{platform}-id" if ok else "", error="" if ok else "boom"
        )

    monkeypatch.setattr(publish_mod, "publish_one", fake)


# ------------------------------------------------------------ the core rule


def test_host_is_released_only_when_all_three_confirm(workspace, monkeypatch):
    video, hosted = workspace
    _results(monkeypatch, {"instagram": True, "youtube": True, "tiktok": True})

    assert publish_mod.run(video) == 0
    assert hosted == {}, "host should be released once every platform confirmed"


@pytest.mark.parametrize(
    "outcome",
    [
        {"instagram": True, "youtube": True, "tiktok": False},
        {"instagram": False, "youtube": True, "tiktok": True},
        {"instagram": True, "youtube": False, "tiktok": False},
    ],
)
def test_any_failure_keeps_the_video_hosted(workspace, monkeypatch, outcome):
    """One platform short is still not done — the URL must survive for the retry."""
    video, hosted = workspace
    _results(monkeypatch, outcome)

    assert publish_mod.run(video) == 1
    assert video.name in hosted


def test_a_retry_that_completes_the_set_releases_the_host(workspace, monkeypatch):
    video, hosted = workspace

    _results(monkeypatch, {"instagram": True, "youtube": True, "tiktok": False})
    publish_mod.run(video)
    assert video.name in hosted

    _results(monkeypatch, {"instagram": True, "youtube": True, "tiktok": True})
    assert publish_mod.run(video) == 0
    assert hosted == {}


def test_already_published_platforms_are_not_posted_again(workspace, monkeypatch):
    """Resuming must never double-post to a platform that already succeeded."""
    video, _ = workspace
    _results(monkeypatch, {"instagram": True, "youtube": True, "tiktok": False})
    publish_mod.run(video)

    attempted: list[str] = []

    def recording(platform, video_, url, dry_run):
        attempted.append(platform)
        return PublishResult(True, post_id=f"{platform}-id")

    monkeypatch.setattr(publish_mod, "publish_one", recording)
    publish_mod.run(video)
    assert attempted == ["tiktok"]


def test_the_host_url_is_stable_across_retries(workspace, monkeypatch):
    video, _ = workspace
    _results(monkeypatch, {"instagram": False, "youtube": False, "tiktok": False})
    publish_mod.run(video)
    first = publish_mod.load_state()["a-reel"]["hosted_url"]

    publish_mod.run(video)
    assert publish_mod.load_state()["a-reel"]["hosted_url"] == first


# ------------------------------------------------------------------- state


def test_failures_are_recorded_with_their_reason(workspace, monkeypatch):
    video, _ = workspace
    _results(monkeypatch, {"instagram": True, "youtube": False, "tiktok": True})
    publish_mod.run(video)

    entry = publish_mod.load_state()["a-reel"]
    assert entry["platforms"]["youtube"]["status"] == "failed"
    assert entry["platforms"]["youtube"]["error"] == "boom"
    assert entry["platforms"]["instagram"]["id"] == "instagram-id"


def test_state_survives_a_corrupt_file(workspace, monkeypatch):
    """A truncated write must not wedge the pipeline."""
    publish_mod.STATE_PATH.write_text("{ not json")
    assert publish_mod.load_state() == {}


def test_all_published_needs_every_platform():
    entry = {
        "platforms": {
            "instagram": {"status": "published"},
            "youtube": {"status": "published"},
            "tiktok": {"status": "pending"},
        }
    }
    assert not publish_mod.all_published(entry)
    entry["platforms"]["tiktok"]["status"] = "published"
    assert publish_mod.all_published(entry)


# ---------------------------------------------------------------- captions


def test_each_platform_gets_its_own_caption(workspace):
    video, _ = workspace
    assert "#tagone" in publish_mod.read_caption(video, "instagram")
    assert "#tagone" not in publish_mod.read_caption(video, "tiktok")
    assert "#Shorts" in publish_mod.read_caption(video, "youtube")


def test_a_caption_section_does_not_bleed_into_the_next(workspace):
    video, _ = workspace
    assert "---" not in publish_mod.read_caption(video, "instagram")


def test_a_missing_caption_file_falls_back_to_the_slug(workspace):
    video, _ = workspace
    video.with_suffix(".caption.txt").unlink()
    assert publish_mod.read_caption(video, "instagram") == "a reel"


# ------------------------------------------------------------------- sweep


def test_sweep_releases_a_host_stranded_too_long(workspace, monkeypatch):
    video, hosted = workspace
    _results(monkeypatch, {"instagram": True, "youtube": False, "tiktok": False})
    publish_mod.run(video)
    assert video.name in hosted

    state = publish_mod.load_state()
    stale = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        days=publish_mod.STRANDED_AFTER_DAYS + 1
    )
    state["a-reel"]["hosted_at"] = stale.isoformat(timespec="seconds")
    publish_mod.save_state(state)

    publish_mod.sweep()
    assert hosted == {}
    assert publish_mod.load_state()["a-reel"]["cleaned_up"]


def test_sweep_leaves_a_recent_failure_alone(workspace, monkeypatch):
    """A publish that failed this morning is a retry candidate, not litter."""
    video, hosted = workspace
    _results(monkeypatch, {"instagram": True, "youtube": False, "tiktok": False})
    publish_mod.run(video)

    publish_mod.sweep()
    assert video.name in hosted


def test_sweep_dry_run_changes_nothing(workspace, monkeypatch):
    video, hosted = workspace
    _results(monkeypatch, {"instagram": False, "youtube": False, "tiktok": False})
    publish_mod.run(video)

    state = publish_mod.load_state()
    stale = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=99)
    state["a-reel"]["hosted_at"] = stale.isoformat(timespec="seconds")
    publish_mod.save_state(state)

    publish_mod.sweep(dry_run=True)
    assert video.name in hosted
    assert not publish_mod.load_state()["a-reel"]["cleaned_up"]
