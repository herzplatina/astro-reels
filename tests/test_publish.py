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

    def fake_push(v):
        hosted[v.name] = True
        return f"https://host/{v.name}"

    # Stub every hosting entry point run() can reach, not just the one it
    # happens to use today: conftest turns an unstubbed call into a failure,
    # but a stub that covers the wrong function would silently do nothing.
    monkeypatch.setattr(publish_mod.hosting, "push", fake_push)
    monkeypatch.setattr(
        publish_mod.hosting, "publish", lambda v, wait=True: fake_push(v)
    )
    monkeypatch.setattr(publish_mod.hosting, "wait_until_live", lambda url: True)
    monkeypatch.setattr(
        publish_mod.hosting,
        "unpublish",
        lambda name: hosted.pop(name, None) is not None,
    )
    monkeypatch.setattr(
        publish_mod.hosting, "public_url", lambda name: f"https://host/{name}"
    )
    # These tests drive the state machine, not the human gate; the gate has its
    # own tests below.
    monkeypatch.setattr(publish_mod, "confirm_approval", lambda v, t, y: None)
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


# --------------------------------------------------- failure classification


def _failing(monkeypatch, mapping):
    """mapping: platform -> (ok, permanent)"""

    def fake(platform, video, url, dry_run):
        ok, permanent = mapping[platform]
        return PublishResult(
            ok,
            post_id=f"{platform}-id" if ok else "",
            error="" if ok else "boom",
            permanent=permanent,
        )

    monkeypatch.setattr(publish_mod, "publish_one", fake)


def test_a_permanent_failure_is_not_retried(workspace, monkeypatch):
    """A refusal will be refused identically; retrying only wastes attempts."""
    video, _ = workspace
    _failing(
        monkeypatch,
        {"instagram": (False, True), "youtube": (True, False), "tiktok": (True, False)},
    )
    publish_mod.run(video)

    attempted = []
    monkeypatch.setattr(
        publish_mod,
        "publish_one",
        lambda p, v, u, d: attempted.append(p) or PublishResult(True, post_id="x"),
    )
    publish_mod.run(video)
    assert attempted == [], "a permanent failure must not be retried automatically"


def test_the_attempt_ceiling_is_five(workspace, monkeypatch):
    """Pinned to a literal.

    Reading MAX_ATTEMPTS on both sides of the assertion made it
    self-referential: raising the constant to 50 kept the test green while the
    retry loop hammered a broken platform ten times longer.
    """
    assert publish_mod.MAX_ATTEMPTS == 5

    video, _ = workspace
    _failing(
        monkeypatch,
        {
            "instagram": (False, False),
            "youtube": (True, False),
            "tiktok": (True, False),
        },
    )
    for _ in range(8):
        publish_mod.run(video)

    entry = publish_mod.load_state()["a-reel"]
    assert entry["platforms"]["instagram"]["attempts"] == 5
    assert "instagram" in publish_mod.blocked_platforms(entry)


def test_a_blocked_reel_stays_hosted(workspace, monkeypatch):
    """Being stuck must never quietly release the file."""
    video, hosted = workspace
    _failing(
        monkeypatch,
        {"instagram": (False, True), "youtube": (True, False), "tiktok": (True, False)},
    )
    publish_mod.run(video)
    assert publish_mod.run(video) == 1
    assert video.name in hosted


def test_attempts_accumulate_across_runs(workspace, monkeypatch):
    video, _ = workspace
    _failing(
        monkeypatch,
        {
            "instagram": (False, False),
            "youtube": (False, False),
            "tiktok": (False, False),
        },
    )
    publish_mod.run(video)
    publish_mod.run(video)
    entry = publish_mod.load_state()["a-reel"]
    assert entry["platforms"]["tiktok"]["attempts"] == 2


# ------------------------------------------------------ crash-safe cleanup


def test_cleanup_still_happens_after_a_crash_before_unpublish(workspace, monkeypatch):
    """The bug this guards: dying between the last publish and the unpublish.

    The next run finds nothing left to publish, so an early return would strand
    the file public with no route back.
    """
    video, hosted = workspace
    _failing(monkeypatch, {p: (True, False) for p in publish_mod.PLATFORMS})
    publish_mod.run(video)

    # Simulate the crash: every platform confirmed, but cleanup never ran.
    state = publish_mod.load_state()
    state["a-reel"]["cleaned_up"] = False
    publish_mod.save_state(state)
    hosted[video.name] = True

    assert publish_mod.run(video) == 0
    assert hosted == {}, "cleanup must be reachable when nothing is left to publish"


def test_release_host_is_idempotent(workspace, monkeypatch):
    """Counts the unpublish calls: a second release attempt is a force-push
    against a file that is already gone."""
    video, hosted = workspace
    calls: list[str] = []
    monkeypatch.setattr(
        publish_mod.hosting,
        "unpublish",
        lambda name: calls.append(name) or (hosted.pop(name, None) is not None),
    )
    _failing(monkeypatch, {p: (True, False) for p in publish_mod.PLATFORMS})
    publish_mod.run(video)
    publish_mod.run(video)
    publish_mod.run(video)
    assert calls == [video.name]
    assert hosted == {}


# ------------------------------------------------------------ escape hatch


def test_abandon_releases_a_permanently_stuck_reel(workspace, monkeypatch):
    video, hosted = workspace
    _failing(
        monkeypatch,
        {"instagram": (False, True), "youtube": (True, False), "tiktok": (True, False)},
    )
    publish_mod.run(video)
    assert video.name in hosted

    publish_mod.abandon("a-reel")
    assert hosted == {}
    assert publish_mod.load_state()["a-reel"]["abandoned"]


def test_sweep_ignores_an_abandoned_reel(workspace, monkeypatch):
    """`abandoned` must be the deciding condition. Abandoning also sets
    cleaned_up, which was the flag actually doing the skipping — so removing the
    abandoned guard changed nothing."""
    video, _ = workspace
    _failing(monkeypatch, {p: (False, True) for p in publish_mod.PLATFORMS})
    publish_mod.run(video)
    publish_mod.abandon("a-reel")

    state = publish_mod.load_state()
    state["a-reel"]["cleaned_up"] = False  # only `abandoned` can skip it now
    state["a-reel"]["hosted_at"] = "2000-01-01T00:00:00+00:00"
    publish_mod.save_state(state)

    calls: list[str] = []
    monkeypatch.setattr(
        publish_mod.hosting, "unpublish", lambda name: calls.append(name) or True
    )
    publish_mod.sweep()
    assert calls == [], "an abandoned reel must not be swept again"


def test_explicit_retry_clears_a_permanent_block(workspace, monkeypatch):
    """After fixing credentials, --retry must be able to try again."""
    video, _ = workspace
    _failing(
        monkeypatch,
        {"instagram": (False, True), "youtube": (True, False), "tiktok": (True, False)},
    )
    publish_mod.run(video)

    entry = publish_mod.load_state()["a-reel"]
    assert "instagram" in publish_mod.blocked_platforms(entry)

    for platform in publish_mod.pending_platforms(entry):
        entry["platforms"][platform]["permanent"] = False
        entry["platforms"][platform]["attempts"] = 0
    assert publish_mod.retryable_platforms(entry) == ["instagram"]


# ------------------------------------------------------- edge and error paths


def test_only_never_reposts_an_already_published_platform(workspace, monkeypatch):
    """--only is an override of *which* to try, not permission to post twice."""
    video, _ = workspace
    _results(monkeypatch, {p: True for p in publish_mod.PLATFORMS})
    publish_mod.run(video)

    attempted = []
    monkeypatch.setattr(
        publish_mod,
        "publish_one",
        lambda p, v, u, d: attempted.append(p) or PublishResult(True, post_id="x"),
    )
    publish_mod.run(video, only=["instagram"])
    assert attempted == []


def test_state_is_written_atomically(workspace, monkeypatch):
    """Injects the crash rather than assuming it. A plain truncating write
    passes a round-trip check, so only a real failure mid-write discriminates.
    """
    good = {"kept": {"platforms": {}}}
    publish_mod.save_state(good)

    real_dump = publish_mod.json.dump

    def explode(obj, fh, **kwargs):
        fh.write('{"half wri')
        raise OSError("no space left on device")

    monkeypatch.setattr(publish_mod.json, "dump", explode)
    with pytest.raises(OSError):
        publish_mod.save_state({"new": {}})
    monkeypatch.setattr(publish_mod.json, "dump", real_dump)

    # The previous good state must still be intact and parseable.
    assert publish_mod.load_state() == good
    leftovers = list(publish_mod.STATE_PATH.parent.glob("*.tmp"))
    assert leftovers == [], f"temp files left behind: {leftovers}"


def test_a_corrupt_state_file_is_preserved_not_discarded(workspace, capsys):
    """Silently forgetting history would re-post everything on the next run."""
    publish_mod.STATE_PATH.write_text("{ truncated")
    assert publish_mod.load_state() == {}

    salvaged = list(publish_mod.STATE_PATH.parent.glob("*corrupt*"))
    assert salvaged, "the unreadable file must be kept for inspection"
    assert "posted twice" in capsys.readouterr().err


def test_a_video_outside_the_repo_does_not_crash(tmp_path, monkeypatch):
    """--out accepts any path; relative_to() would raise for anything outside."""
    monkeypatch.setattr(publish_mod, "STATE_PATH", tmp_path / "s.json")
    monkeypatch.setattr(publish_mod, "ROOT", tmp_path / "repo")

    stray = tmp_path / "elsewhere" / "reel.mp4"
    stray.parent.mkdir()
    stray.write_bytes(b"x")

    entry = publish_mod.entry_for({}, stray)
    assert entry["file"] == str(stray)


def test_an_older_state_entry_is_migrated_not_crashed_on(workspace):
    """A schema change must not KeyError halfway through a publish."""
    video, _ = workspace
    state = {"a-reel": {"file": "a-reel.mp4"}}  # pre-dates every other field
    entry = publish_mod.entry_for(state, video)

    assert entry["hosted_url"] == ""
    assert entry["cleaned_up"] is False
    assert set(entry["platforms"]) == set(publish_mod.PLATFORMS)
    assert publish_mod.pending_platforms(entry) == list(publish_mod.PLATFORMS)


def test_reports_survive_a_partial_entry(workspace):
    """--status and --sweep must not crash on a half-written record."""
    publish_mod.save_state({"broken": {}})
    assert publish_mod.show_status() == 0
    assert publish_mod.sweep(dry_run=True) == 0


def test_sweep_tolerates_an_unreadable_timestamp(workspace, monkeypatch):
    video, hosted = workspace
    _results(monkeypatch, {"instagram": True, "youtube": False, "tiktok": False})
    publish_mod.run(video)

    state = publish_mod.load_state()
    state["a-reel"]["hosted_at"] = "not-a-date"
    publish_mod.save_state(state)

    publish_mod.sweep()
    assert hosted == {}, "a bad timestamp must not leave the file hosted forever"


def test_a_timed_out_pages_build_still_records_the_host(workspace, monkeypatch):
    """The file is public the moment it is pushed, so it must be recorded then.

    Recording only after the availability wait would orphan it: hosted publicly,
    with no entry for --status or --sweep to find.
    """
    video, _ = workspace
    pushed = {}
    monkeypatch.setattr(
        publish_mod.hosting,
        "push",
        lambda v: (
            pushed.setdefault(v.name, True)
            and f"https://host/{v.name}"
            or f"https://host/{v.name}"
        ),
    )
    monkeypatch.setattr(publish_mod.hosting, "wait_until_live", lambda url: False)

    with pytest.raises(SystemExit):
        publish_mod.run(video)

    entry = publish_mod.load_state()["a-reel"]
    assert pushed, "the file was pushed"
    assert entry["hosted_url"], "and the state must know about it"
    assert entry["hosted_at"]


def test_youtube_never_gets_an_empty_title(workspace, monkeypatch):
    video, _ = workspace
    video.with_suffix(".caption.txt").write_text("--- YOUTUBE ---\n\n\n#Shorts\n")

    captured = {}
    monkeypatch.setattr(
        publish_mod,
        "publish_youtube",
        lambda v, title, desc: (
            captured.setdefault("title", title) or PublishResult(True, post_id="y")
        ),
    )
    publish_mod.publish_one("youtube", video, "https://host/x", dry_run=False)
    assert captured["title"].strip()


# ------------------------------------------- regressions found by the review


def test_dry_run_never_writes_state(workspace, monkeypatch):
    """The bug this guards is the worst one found: a rehearsal recording itself
    as real, which made the subsequent real publish a silent no-op."""
    video, _ = workspace
    _results(monkeypatch, {p: True for p in publish_mod.PLATFORMS})

    publish_mod.run(video, dry_run=True)
    assert not publish_mod.STATE_PATH.exists()
    assert publish_mod.load_state() == {}


def test_a_real_run_after_a_dry_run_actually_publishes(workspace, monkeypatch):
    video, _ = workspace
    attempted: list[str] = []

    def recording(platform, v, u, d):
        if not d:
            attempted.append(platform)
        return PublishResult(True, post_id=platform)

    monkeypatch.setattr(publish_mod, "publish_one", recording)
    publish_mod.run(video, dry_run=True)
    publish_mod.run(video)
    assert sorted(attempted) == sorted(publish_mod.PLATFORMS)


def test_save_state_honours_dry_run_directly(workspace):
    publish_mod.save_state({"x": 1}, dry_run=True)
    assert not publish_mod.STATE_PATH.exists()
    publish_mod.save_state({"x": 1}, dry_run=False)
    assert publish_mod.STATE_PATH.exists()


def test_only_respects_the_attempt_ceiling(workspace, monkeypatch):
    """--only chooses which platforms to try; it is not a licence to ignore a
    permanent refusal."""
    video, _ = workspace
    _failing(
        monkeypatch,
        {"instagram": (False, True), "youtube": (True, False), "tiktok": (True, False)},
    )
    publish_mod.run(video)

    attempted = []
    monkeypatch.setattr(
        publish_mod,
        "publish_one",
        lambda p, v, u, d: attempted.append(p) or PublishResult(True, post_id="x"),
    )
    publish_mod.run(video, only=["instagram"])
    assert attempted == []


def test_sweep_clears_the_url_so_a_retry_can_rehost(workspace, monkeypatch):
    """Releasing the file while keeping hosted_url set wedged the reel: run()
    only hosts when the URL is empty, so retries hit a dead link forever."""
    video, _ = workspace
    _results(monkeypatch, {"instagram": False, "youtube": True, "tiktok": True})
    publish_mod.run(video)

    state = publish_mod.load_state()
    stale = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=99)
    state["a-reel"]["hosted_at"] = stale.isoformat(timespec="seconds")
    publish_mod.save_state(state)
    publish_mod.sweep()

    assert publish_mod.load_state()["a-reel"]["hosted_url"] == ""


def test_sweep_handles_a_timezone_naive_timestamp(workspace, monkeypatch):
    """It parses cleanly, so ValueError never fired — it died comparing it."""
    video, hosted = workspace
    _results(monkeypatch, {"instagram": True, "youtube": False, "tiktok": False})
    publish_mod.run(video)

    state = publish_mod.load_state()
    state["a-reel"]["hosted_at"] = "2020-01-01T00:00:00"  # naive
    publish_mod.save_state(state)

    publish_mod.sweep()
    assert hosted == {}


def test_retry_migrates_an_entry_missing_a_platform(workspace, monkeypatch):
    """--retry indexed platforms directly and KeyErrored on an older record."""
    video, _ = workspace
    state = {
        "a-reel": {
            "file": "a-reel.mp4",
            "platforms": {"instagram": {"status": "pending"}},
        }
    }
    entry = publish_mod.entry_for(state, video)
    for platform in publish_mod.pending_platforms(entry):
        entry["platforms"][platform]["permanent"] = False
    assert set(entry["platforms"]) == set(publish_mod.PLATFORMS)


# --------------------------------------------------------- the approval gate


def test_publishing_requires_confirmation(tmp_path, monkeypatch):
    """Nothing may be posted publicly without an explicit go-ahead."""
    video = tmp_path / "a-reel.mp4"
    monkeypatch.setattr(publish_mod.sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit) as caught:
        publish_mod.confirm_approval(video, ["instagram"], assume_yes=False)
    assert "Refusing to publish" in str(caught.value)


def test_typing_anything_but_publish_cancels(tmp_path, monkeypatch):
    video = tmp_path / "a-reel.mp4"
    monkeypatch.setattr(publish_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "yes")
    with pytest.raises(SystemExit) as caught:
        publish_mod.confirm_approval(video, ["instagram"], assume_yes=False)
    assert "Cancelled" in str(caught.value)


def test_typing_publish_proceeds(tmp_path, monkeypatch):
    video = tmp_path / "a-reel.mp4"
    monkeypatch.setattr(publish_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "publish")
    assert publish_mod.confirm_approval(video, ["instagram"], assume_yes=False) is None


def test_yes_flag_skips_the_prompt(tmp_path):
    video = tmp_path / "a-reel.mp4"
    assert publish_mod.confirm_approval(video, ["instagram"], assume_yes=True) is None


# ----------------------------------------- publish_one, without the stub in place


def test_missing_credentials_are_classified_permanent(workspace, monkeypatch):
    """Every orchestration test stubs publish_one wholesale, so the classification
    that gates the whole retry loop was never exercised."""
    video, _ = workspace
    monkeypatch.setattr(
        publish_mod,
        "publish_instagram",
        lambda *a, **k: (_ for _ in ()).throw(
            publish_mod.CredentialsMissing("no token")
        ),
    )
    result = publish_mod.publish_one("instagram", video, "https://h/x", dry_run=False)
    assert not result.ok
    assert result.permanent, "an absent token is not something a retry fixes"


def test_an_unexpected_error_is_classified_transient(workspace, monkeypatch):
    video, _ = workspace
    monkeypatch.setattr(
        publish_mod,
        "publish_youtube",
        lambda *a, **k: (_ for _ in ()).throw(TimeoutError("socket timed out")),
    )
    result = publish_mod.publish_one("youtube", video, "https://h/x", dry_run=False)
    assert not result.ok
    assert not result.permanent
    assert "TimeoutError" in result.error


def test_publish_one_dispatches_to_the_right_client(workspace, monkeypatch):
    video, _ = workspace
    seen: list[str] = []
    monkeypatch.setattr(
        publish_mod,
        "publish_instagram",
        lambda url, caption: seen.append("ig") or PublishResult(True, post_id="i"),
    )
    monkeypatch.setattr(
        publish_mod,
        "publish_youtube",
        lambda v, t, d: seen.append("yt") or PublishResult(True, post_id="y"),
    )
    monkeypatch.setattr(
        publish_mod,
        "publish_tiktok",
        lambda v, c: seen.append("tt") or PublishResult(True, post_id="t"),
    )
    for platform in publish_mod.PLATFORMS:
        publish_mod.publish_one(platform, video, "https://h/x", dry_run=False)
    assert seen == ["ig", "yt", "tt"]


def test_an_unknown_platform_is_reported(workspace):
    video, _ = workspace
    result = publish_mod.publish_one("myspace", video, "https://h/x", dry_run=False)
    assert not result.ok and "unknown platform" in result.error


def test_dry_run_makes_no_client_call(workspace, monkeypatch):
    video, _ = workspace
    for name in ("publish_instagram", "publish_youtube", "publish_tiktok"):
        monkeypatch.setattr(
            publish_mod,
            name,
            lambda *a, **k: pytest.fail("a dry run must not reach a client"),
        )
    assert publish_mod.publish_one("instagram", video, "https://h/x", dry_run=True).ok
