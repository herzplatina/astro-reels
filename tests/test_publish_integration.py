#!/usr/bin/env python3
"""The live publish flow, end to end, with hosting NOT mocked.

Every other publish test stubs `hosting` wholesale, so the seam between
`publish.py` and `hosting.py` is never exercised — a change to either could
break the real flow while the suite stayed green. Here the git side is real,
running against a bare repository on disk, and only the three platform clients
are replaced, because they are the one genuinely external dependency.

The question these answer: after N publish cycles, is anything left behind?
Files in `reels/`, commits on the branch, objects in the local clone. Hosting a
video is a public, force-pushed side effect, so growth here is not a tidiness
problem — it is unbounded public storage.

    python3 -m pytest tests/test_publish_integration.py -q
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import hosting  # noqa: E402
import publish as publish_mod  # noqa: E402
from platforms import PublishResult  # noqa: E402

pytestmark = pytest.mark.allow_network  # real git, local only


def git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def live(tmp_path, monkeypatch):
    """A real hosting branch plus a real state file; only platforms are stubbed."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git("init", "--bare", "--initial-branch=main", cwd=origin)
    # hosting clones with --depth 1, and a bare repo rejects a shallow push by
    # default. GitHub accepts one, so match that rather than deepening the
    # clone and testing a flow we do not run.
    git("config", "receive.shallowUpdate", "true", cwd=origin)

    seed = tmp_path / "seed"
    seed.mkdir()
    git("init", "--initial-branch=main", cwd=seed)
    git("config", "user.email", "t@t", cwd=seed)
    git("config", "user.name", "t", cwd=seed)
    (seed / "README.md").write_text("source\n")
    git("add", "-A", cwd=seed)
    git("commit", "-m", "main", cwd=seed)
    git("remote", "add", "origin", str(origin), cwd=seed)
    git("push", "-u", "origin", "main", cwd=seed)
    git("checkout", "--orphan", "gh-pages", cwd=seed)
    git("rm", "-rf", ".", cwd=seed)
    (seed / "index.html").write_text("root\n")
    git("add", "-A", cwd=seed)
    git("commit", "-m", "hosting root", cwd=seed)
    git("push", "-u", "origin", "gh-pages", cwd=seed)

    monkeypatch.setattr(hosting, "CLONE", tmp_path / "clone")
    monkeypatch.setattr(hosting, "remote_url", lambda: str(origin))
    monkeypatch.setattr(hosting, "pages_base_url", lambda: "https://u.github.io/r/")
    # GitHub's CDN is the one thing that cannot be stood up locally.
    monkeypatch.setattr(hosting, "wait_until_live", lambda url, timeout=300: True)

    monkeypatch.setattr(publish_mod, "STATE_PATH", tmp_path / "publish_state.json")
    monkeypatch.setattr(publish_mod, "ROOT", tmp_path)

    class World:
        origin_path = origin
        workdir = tmp_path

        def video(self, name: str, text: str = "guidance") -> Path:
            path = tmp_path / name
            path.write_bytes(b"\x00" * 2048)
            path.with_suffix(".caption.txt").write_text(
                f"--- INSTAGRAM ---\n{text}\n\n#a\n\n"
                f"--- TIKTOK ---\n{text}\n\n#b\n\n"
                f"--- YOUTUBE ---\n{text}\n\n#c\n"
            )
            return path

        def hosted(self) -> list[str]:
            out = subprocess.run(
                ["git", "ls-tree", "-r", "--name-only", "gh-pages"],
                cwd=origin,
                capture_output=True,
                text=True,
            ).stdout.split()
            return sorted(f for f in out if f.startswith("reels/"))

        def depth(self, branch="gh-pages") -> int:
            return int(git("rev-list", "--count", branch, cwd=origin))

    return World()


def platforms_return(monkeypatch, mapping):
    def fake(platform, video, url, dry_run):
        ok = mapping[platform]
        return PublishResult(
            ok, post_id=f"{platform}-id" if ok else "", error="" if ok else "refused"
        )

    monkeypatch.setattr(publish_mod, "publish_one", fake)


ALL_OK = {"instagram": True, "youtube": True, "tiktok": True}


# ------------------------------------------------------------- the full flow


def test_a_successful_publish_leaves_nothing_hosted(live, monkeypatch):
    video = live.video("reel.mp4")
    platforms_return(monkeypatch, ALL_OK)

    assert publish_mod.run(video, assume_yes=True) == 0
    assert live.hosted() == []
    assert live.depth() == 1


def test_the_video_is_genuinely_public_while_publishing(live, monkeypatch):
    """Instagram fetches the file mid-run, so it must really be on the branch
    at the moment the platform is called — not merely recorded as hosted."""
    video = live.video("reel.mp4")
    seen: list[list[str]] = []

    def fake(platform, v, url, dry_run):
        seen.append(live.hosted())
        return PublishResult(True, post_id=platform)

    monkeypatch.setattr(publish_mod, "publish_one", fake)
    publish_mod.run(video, assume_yes=True)

    assert seen and all(s == ["reels/reel.mp4"] for s in seen)
    assert live.hosted() == []


def test_a_partial_failure_really_leaves_the_file_on_the_branch(live, monkeypatch):
    video = live.video("reel.mp4")
    platforms_return(monkeypatch, {"instagram": True, "youtube": False, "tiktok": True})

    assert publish_mod.run(video, assume_yes=True) == 1
    assert live.hosted() == ["reels/reel.mp4"]


def test_a_retry_completing_the_set_removes_the_real_file(live, monkeypatch):
    video = live.video("reel.mp4")
    platforms_return(monkeypatch, {"instagram": True, "youtube": False, "tiktok": True})
    publish_mod.run(video, assume_yes=True)
    assert live.hosted() == ["reels/reel.mp4"]

    platforms_return(monkeypatch, ALL_OK)
    assert publish_mod.run(video, assume_yes=True) == 0
    assert live.hosted() == []
    assert live.depth() == 1


def test_cleanup_after_a_crash_removes_the_real_file(live, monkeypatch):
    """The state says published but the file is still on the branch."""
    video = live.video("reel.mp4")
    platforms_return(monkeypatch, ALL_OK)
    publish_mod.run(video, assume_yes=True)

    hosting.push(video)  # simulate the crash: hosted again, state says done
    state = publish_mod.load_state()
    state["reel"]["cleaned_up"] = False
    publish_mod.save_state(state)
    assert live.hosted() == ["reels/reel.mp4"]

    assert publish_mod.run(video, assume_yes=True) == 0
    assert live.hosted() == []


# ------------------------------------------------- accumulation over time


def test_twenty_cycles_leave_the_branch_exactly_as_it_started(live, monkeypatch):
    """The headline property: hosting must not grow without bound.

    Twenty publishes is roughly two months at three reels a week.
    """
    platforms_return(monkeypatch, ALL_OK)
    for index in range(20):
        video = live.video(f"reel-{index:02d}.mp4")
        assert publish_mod.run(video, assume_yes=True) == 0

    assert live.hosted() == []
    assert live.depth() == 1
    assert live.depth("main") == 1


def test_repeated_failures_do_not_pile_up_beyond_what_is_outstanding(live, monkeypatch):
    """A run of failures should host each reel once, not once per attempt."""
    platforms_return(monkeypatch, {"instagram": False, "youtube": True, "tiktok": True})
    for index in range(5):
        video = live.video(f"stuck-{index}.mp4")
        publish_mod.run(video, assume_yes=True)

    assert len(live.hosted()) == 5, live.hosted()
    assert live.depth() == 1

    # Retrying the same five must not duplicate anything.
    for index in range(5):
        publish_mod.run(live.video(f"stuck-{index}.mp4"), assume_yes=True)
    assert len(live.hosted()) == 5
    assert live.depth() == 1


def test_the_sweep_really_removes_the_file_and_allows_rehosting(live, monkeypatch):
    import datetime as dt

    video = live.video("reel.mp4")
    platforms_return(monkeypatch, {"instagram": False, "youtube": True, "tiktok": True})
    publish_mod.run(video, assume_yes=True)
    assert live.hosted() == ["reels/reel.mp4"]

    state = publish_mod.load_state()
    stale = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=99)
    state["reel"]["hosted_at"] = stale.isoformat(timespec="seconds")
    publish_mod.save_state(state)

    publish_mod.sweep()
    assert live.hosted() == [], "the sweep must delete the real file"

    # And the reel must be re-hostable rather than wedged against a dead URL.
    entry = publish_mod.load_state()["reel"]
    entry["platforms"]["instagram"] = {"status": "pending"}
    publish_mod.save_state({"reel": entry})
    platforms_return(monkeypatch, ALL_OK)
    publish_mod.run(video, assume_yes=True)
    assert live.hosted() == []


def test_abandon_removes_the_real_file(live, monkeypatch):
    video = live.video("reel.mp4")
    platforms_return(
        monkeypatch, {"instagram": False, "youtube": False, "tiktok": False}
    )
    publish_mod.run(video, assume_yes=True)
    assert live.hosted() == ["reels/reel.mp4"]

    publish_mod.abandon("reel")
    assert live.hosted() == []


def test_the_local_clone_does_not_grow_without_bound(live, monkeypatch):
    """Amended commits leave unreachable objects behind in the working clone.

    Left unchecked this is a directory on your disk that grows every time you
    post, forever.
    """
    platforms_return(monkeypatch, ALL_OK)

    def clone_bytes() -> int:
        return sum(f.stat().st_size for f in hosting.CLONE.rglob("*") if f.is_file())

    publish_mod.run(live.video("warm-up.mp4"), assume_yes=True)
    baseline = clone_bytes()

    for index in range(15):
        publish_mod.run(live.video(f"reel-{index:02d}.mp4"), assume_yes=True)

    # Each video is 2 KB; fifteen of them retained would be plainly visible.
    growth = clone_bytes() - baseline
    assert growth < 15 * 2048, f"clone grew by {growth} bytes over 15 publishes"
