#!/usr/bin/env python3
"""Tests for the shared ffmpeg helpers and the remaining error paths."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import media  # noqa: E402
import prune as prune_mod  # noqa: E402


def test_missing_tools_fail_with_an_instruction(monkeypatch):
    """A missing ffmpeg should say so, not surface as a subprocess traceback."""
    monkeypatch.setattr(media.shutil, "which", lambda tool: None)
    with pytest.raises(SystemExit) as caught:
        media.require_tools("ffmpeg", "ffprobe")
    message = str(caught.value)
    assert "ffmpeg" in message and "brew install" in message


def test_present_tools_pass_silently(monkeypatch):
    monkeypatch.setattr(media.shutil, "which", lambda tool: "/usr/bin/" + tool)
    assert media.require_tools("ffmpeg") is None


def test_probe_duration_of_a_non_media_file_is_zero_not_an_error(tmp_path):
    """Callers use this to pick an offset; it must never abort a render."""
    junk = tmp_path / "junk.mp3"
    junk.write_bytes(b"definitely not audio")
    assert media.probe_duration(junk) == 0.0


def test_probe_duration_of_a_missing_file_is_zero(tmp_path):
    assert media.probe_duration(tmp_path / "gone.mp3") == 0.0


# ------------------------------------------------------------------- prune


def test_prune_survives_a_file_vanishing_mid_run(tmp_path, monkeypatch):
    """A concurrent render or manual delete must not abort the whole prune."""
    monkeypatch.setattr(prune_mod, "OUTPUT_DIR", tmp_path)

    import os
    import time

    stale = time.time() - 999 * 86400
    for name in ("a", "b"):
        path = tmp_path / f"{name}.mp4"
        path.write_bytes(b"x" * 512)
        os.utime(path, (stale, stale))

    real_glob = Path.glob

    def glob_then_delete(self, pattern):
        found = list(real_glob(self, pattern))
        # Whip one file away after globbing, before it can be stat'd.
        if found:
            found[0].unlink(missing_ok=True)
        return iter(found)

    monkeypatch.setattr(Path, "glob", glob_then_delete)
    removed, _ = prune_mod.prune(days=1, keep_minimum=0)
    assert len(removed) == 1  # the survivor, without raising on the other
