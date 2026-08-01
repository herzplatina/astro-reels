#!/usr/bin/env python3
"""Tests for the pure logic: duration, slugs, wrapping, fitting, retention.

Deliberately no ffmpeg here — rendering is verified by watching a reel. What is
worth testing is the arithmetic that decides how long it runs, where it is
written, and what gets deleted.

    python3 -m pytest tests/ -q
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import make_reel  # noqa: E402
import prune as prune_mod  # noqa: E402


@pytest.fixture
def cfg() -> dict:
    return make_reel.load_config()


# ---------------------------------------------------------------- duration


def test_duration_scales_with_word_count(cfg):
    short = make_reel.compute_duration("Three little words.", cfg, None)
    long = make_reel.compute_duration(" ".join(["word"] * 25), cfg, None)
    assert short < long


def test_duration_is_clamped_to_configured_bounds(cfg):
    bounds = cfg["duration"]
    for words in (1, 5, 50, 500):
        seconds = make_reel.compute_duration(" ".join(["w"] * words), cfg, None)
        assert bounds["min_seconds"] <= seconds <= bounds["max_seconds"]


def test_computed_duration_always_lands_in_the_reels_window(cfg):
    """The configured clamp must stay inside what Instagram will surface."""
    for words in (1, 500):
        seconds = make_reel.compute_duration(" ".join(["w"] * words), cfg, None)
        assert make_reel.PLATFORM_MIN_SECONDS <= seconds
        assert seconds <= make_reel.PLATFORM_MAX_SECONDS


def test_duration_override_is_honoured(cfg):
    assert make_reel.compute_duration("anything", cfg, 12.5) == 12.5


@pytest.mark.parametrize("bad", [0, -1, -0.5])
def test_non_positive_duration_override_is_rejected(cfg, bad):
    with pytest.raises(SystemExit):
        make_reel.compute_duration("anything", cfg, bad)


def test_off_spec_override_warns_but_proceeds(cfg, capsys):
    assert make_reel.compute_duration("anything", cfg, 300) == 300
    assert "Reels" in capsys.readouterr().err


# ------------------------------------------------------------------- slugs


def test_slugify_strips_punctuation_and_case():
    assert make_reel.slugify("Trust the Timing!") == "trust-the-timing"


def test_slugify_handles_accents_and_emoji():
    assert make_reel.slugify("Café ✨ wisdom") == "cafe-wisdom"


def test_slugify_never_returns_empty():
    assert make_reel.slugify("!!!???") == "reel"
    assert make_reel.slugify("") == "reel"


def test_differing_texts_with_a_shared_opening_do_not_collide(tmp_path):
    """Texts that genuinely collide at the 40-char truncation.

    The earlier pair diverged before character 40, so their slugs already
    differed and the collision branch was never entered at all.
    """
    a = "Today's guidance is to rest and let the answers arrive on their own."
    b = "Today's guidance is to rest and let the world keep turning without you."
    assert make_reel.slugify(a) == make_reel.slugify(b), "must actually collide"

    first = make_reel.unique_output_path(tmp_path, a)
    first.touch()
    first.with_suffix(".caption.txt").write_text(a)

    second = make_reel.unique_output_path(tmp_path, b)
    assert second != first


def test_rerendering_identical_text_reuses_the_same_path(tmp_path):
    text = "The same words twice."
    first = make_reel.unique_output_path(tmp_path, text)
    first.touch()
    first.with_suffix(".caption.txt").write_text(text)

    assert make_reel.unique_output_path(tmp_path, text) == first


# ----------------------------------------------------------------- wrapping


def _probe():
    return ImageDraw.Draw(Image.new("RGB", (10, 10)))


def test_wrap_respects_the_width_budget(cfg):
    font = make_reel.load_font(cfg, 60)
    max_width = 800
    lines = make_reel.wrap_to_width(
        "The moon in your chart is asking for rest, not answers.",
        font,
        max_width,
        _probe(),
    )
    assert len(lines) > 1
    for line in lines:
        assert _probe().textlength(line, font=font) <= max_width


def test_wrap_preserves_explicit_newlines(cfg):
    font = make_reel.load_font(cfg, 40)
    lines = make_reel.wrap_to_width("alpha\nbeta", font, 10_000, _probe())
    assert lines == ["alpha", "beta"]


def test_wrap_keeps_an_unbreakable_word_rather_than_dropping_it(cfg):
    font = make_reel.load_font(cfg, 60)
    lines = make_reel.wrap_to_width("supercalifragilistic", font, 10, _probe())
    assert lines == ["supercalifragilistic"]


def test_no_words_are_lost_in_wrapping(cfg):
    font = make_reel.load_font(cfg, 60)
    text = "Some seasons are for listening rather than for asking questions."
    lines = make_reel.wrap_to_width(text, font, 500, _probe())
    assert " ".join(lines).split() == text.split()


# ------------------------------------------------------------------ fitting


def test_fit_shrinks_long_text_below_the_maximum_size(cfg):
    canvas = (cfg["video"]["width"], cfg["video"]["height"])
    short_font, _ = make_reel.fit_text("Short.", cfg, canvas)
    long_font, _ = make_reel.fit_text(" ".join(["word"] * 60), cfg, canvas)
    assert long_font.size < short_font.size


def test_overlong_text_warns_instead_of_cropping_silently(cfg, capsys):
    canvas = (cfg["video"]["width"], cfg["video"]["height"])
    make_reel.fit_text(" ".join(["word"] * 400), cfg, canvas)
    assert "too long" in capsys.readouterr().err


def test_fit_never_exceeds_the_configured_font_ceiling(cfg):
    canvas = (cfg["video"]["width"], cfg["video"]["height"])
    font, _ = make_reel.fit_text("Hi.", cfg, canvas)
    assert font.size <= cfg["text"]["max_size"]


# ---------------------------------------------------------------- retention


def _reel(directory: Path, name: str, age_days: float) -> Path:
    path = directory / f"{name}.mp4"
    path.write_bytes(b"x" * 1024)
    path.with_suffix(".caption.txt").write_text("caption")
    stamp = time.time() - age_days * 86400
    for target in (path, path.with_suffix(".caption.txt")):
        import os

        os.utime(target, (stamp, stamp))
    return path


@pytest.fixture
def output_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(prune_mod, "OUTPUT_DIR", tmp_path)
    return tmp_path


def test_prune_removes_only_what_is_past_the_cutoff(output_dir):
    old = _reel(output_dir, "old", age_days=90)
    fresh = _reel(output_dir, "fresh", age_days=1)

    removed, _ = prune_mod.prune(days=60, keep_minimum=0)

    assert removed == [old]
    assert not old.exists()
    assert fresh.exists()


def test_prune_deletes_the_caption_alongside_the_reel(output_dir):
    old = _reel(output_dir, "old", age_days=90)
    prune_mod.prune(days=60, keep_minimum=0)
    assert not old.with_suffix(".caption.txt").exists()


def test_keep_minimum_protects_the_newest_regardless_of_age(output_dir):
    """Asserts WHICH files survive, not just how many.

    Counting alone cannot tell "keep the newest 3" from "keep the oldest 3", so
    reversing the sort — which would delete your most recent reels — went
    undetected.
    """
    for i in range(5):
        _reel(output_dir, f"age-{i}", age_days=365 + i)  # age-0 newest, age-4 oldest

    removed, _ = prune_mod.prune(days=60, keep_minimum=3)

    assert sorted(p.stem for p in removed) == ["age-3", "age-4"]
    survivors = sorted(p.stem for p in output_dir.glob("*.mp4"))
    assert survivors == ["age-0", "age-1", "age-2"]


def test_dry_run_reports_without_deleting(output_dir):
    old = _reel(output_dir, "old", age_days=90)
    removed, freed = prune_mod.prune(days=60, keep_minimum=0, dry_run=True)

    assert removed == [old]
    assert freed > 0
    assert old.exists()


def test_prune_on_a_missing_directory_is_a_no_op(tmp_path, monkeypatch):
    monkeypatch.setattr(prune_mod, "OUTPUT_DIR", tmp_path / "nope")
    assert prune_mod.prune(days=60, keep_minimum=0) == ([], 0.0)


def test_a_text_that_is_a_prefix_of_another_does_not_overwrite_it(tmp_path):
    """Both truncate to the same 40-char slug. A substring comparison handed
    back the other reel's path and ffmpeg -y destroyed it."""
    longer = "The moon in your chart shapes your emotional weather patterns deeply"
    shorter = "The moon in your chart shapes your emotional weather"
    assert make_reel.slugify(longer) == make_reel.slugify(shorter)

    first = make_reel.unique_output_path(tmp_path, longer)
    first.touch()
    first.with_suffix(".caption.txt").write_text(
        f"--- INSTAGRAM ---\n{longer}\n\n#tag\n"
    )

    second = make_reel.unique_output_path(tmp_path, shorter)
    assert second != first


def test_fit_text_actually_steps_down_through_intermediate_sizes(cfg):
    """Asserts an intermediate size, not merely "smaller".

    Disabling the shrink loop still produced a smaller font for long text,
    because control fell through to the min_size fallback — so a test comparing
    long-vs-short passed with the loop entirely broken.
    """
    canvas = (cfg["video"]["width"], cfg["video"]["height"])
    largest = cfg["text"]["max_size"]
    smallest = cfg["text"]["min_size"]

    for words in range(4, 40, 2):
        font, _ = make_reel.fit_text(" ".join(["word"] * words), cfg, canvas)
        if smallest < font.size < largest:
            return
    pytest.fail("no text length produced a size between min_size and max_size")
