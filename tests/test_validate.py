#!/usr/bin/env python3
"""Tests for the pre-flight checks.

Built on synthetic images rather than the real background, so the thresholds are
tested against known geometry and the suite does not break when the artwork is
swapped.

    python3 -m pytest tests/ -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import make_reel  # noqa: E402
import validate as validate_mod  # noqa: E402

FRAME = (1080, 1920)


@pytest.fixture
def cfg() -> dict:
    return make_reel.load_config()


def _layer_with_text_at(box: tuple[int, int, int, int]) -> Image.Image:
    """An RGBA layer with a fully opaque block standing in for glyphs."""
    layer = Image.new("RGBA", FRAME, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rectangle(box, fill=(255, 255, 255, 255))
    return layer


def _flat(colour: tuple[int, int, int]) -> Image.Image:
    return Image.new("RGB", FRAME, colour)


# ------------------------------------------------------------ contrast maths


def test_contrast_ratio_extremes():
    assert validate_mod.contrast_ratio((255, 255, 255), (0, 0, 0)) == pytest.approx(
        21, abs=0.1
    )
    assert validate_mod.contrast_ratio(
        (128, 128, 128), (128, 128, 128)
    ) == pytest.approx(1.0)


def test_contrast_ratio_is_symmetric():
    a = validate_mod.contrast_ratio((255, 255, 255), (40, 40, 40))
    b = validate_mod.contrast_ratio((40, 40, 40), (255, 255, 255))
    assert a == pytest.approx(b)


# ------------------------------------------------------------ visible region


def test_no_zoom_leaves_the_whole_frame_visible():
    assert validate_mod.visible_region(FRAME, 1.0) == (0, 0, 1080, 1920)


def test_zoom_insets_the_visible_region_on_all_sides():
    left, top, right, bottom = validate_mod.visible_region(FRAME, 1.06)
    assert left > 0 and top > 0
    assert right < 1080 and bottom < 1920
    # Symmetric, because the push-in is centred.
    assert left == 1080 - right
    assert top == 1920 - bottom


def test_stronger_zoom_crops_more():
    mild = validate_mod.visible_region(FRAME, 1.05)
    strong = validate_mod.visible_region(FRAME, 1.30)
    assert strong[0] > mild[0]


# ------------------------------------------------------------------ overflow


def test_centred_text_passes(cfg):
    layer = _layer_with_text_at((300, 800, 780, 1000))
    assert validate_mod.check_overflow(layer, cfg, 1.06) == []


def test_text_past_the_left_edge_is_caught(cfg):
    layer = _layer_with_text_at((2, 800, 780, 1000))
    problems = validate_mod.check_overflow(layer, cfg, 1.06)
    assert [p.check for p in problems] == ["overflow"]
    assert "left" in problems[0].message


def test_text_below_the_bottom_edge_is_caught(cfg):
    layer = _layer_with_text_at((300, 1700, 780, 1918))
    problems = validate_mod.check_overflow(layer, cfg, 1.06)
    assert "bottom" in problems[0].message


def test_zoom_makes_the_safe_area_stricter(cfg):
    """Text that survives no zoom can still be cropped by a push-in."""
    layer = _layer_with_text_at((300, 105, 780, 400))
    assert validate_mod.check_overflow(layer, cfg, 1.0) == []
    assert validate_mod.check_overflow(layer, cfg, 1.30) != []


def test_an_empty_layer_is_reported(cfg):
    blank = Image.new("RGBA", FRAME, (0, 0, 0, 0))
    problems = validate_mod.check_overflow(blank, cfg, 1.06)
    assert [p.check for p in problems] == ["empty"]


# -------------------------------------------------------- subject collision


def test_text_clear_of_the_subject_passes(cfg):
    layer = _layer_with_text_at((300, 300, 780, 500))
    background = _flat((0, 0, 0))
    ImageDraw.Draw(background).rectangle((300, 1400, 780, 1700), fill=(255, 200, 80))
    assert validate_mod.check_subject_collision(layer, background, cfg) == []


def test_text_over_the_lit_subject_is_caught(cfg):
    layer = _layer_with_text_at((300, 1450, 780, 1650))
    background = _flat((0, 0, 0))
    ImageDraw.Draw(background).rectangle((300, 1400, 780, 1700), fill=(255, 200, 80))
    problems = validate_mod.check_subject_collision(layer, background, cfg)
    assert [p.check for p in problems] == ["subject-collision"]


def test_a_wholly_dark_background_has_no_subject_to_collide_with(cfg):
    layer = _layer_with_text_at((300, 800, 780, 1000))
    assert validate_mod.check_subject_collision(layer, _flat((0, 0, 0)), cfg) == []


# ------------------------------------------------------------------ contrast


def test_white_text_on_black_passes(cfg):
    layer = _layer_with_text_at((300, 800, 780, 1000))
    assert validate_mod.check_contrast(layer, _flat((0, 0, 0)), cfg) == []


def test_white_text_on_white_is_caught(cfg):
    layer = _layer_with_text_at((300, 800, 780, 1000))
    problems = validate_mod.check_contrast(layer, _flat((255, 255, 255)), cfg)
    assert [p.check for p in problems] == ["contrast"]


def test_white_text_on_mid_grey_is_caught(cfg):
    """The classic near-miss: readable-ish on a monitor, mush on a phone."""
    layer = _layer_with_text_at((300, 800, 780, 1000))
    problems = validate_mod.check_contrast(layer, _flat((150, 150, 150)), cfg)
    assert [p.check for p in problems] == ["contrast"]


def test_contrast_is_measured_only_under_the_glyphs(cfg):
    """A bright patch elsewhere in the frame must not rescue dark-on-dark text."""
    background = _flat((10, 10, 10))
    ImageDraw.Draw(background).rectangle((0, 0, 1080, 600), fill=(255, 255, 255))
    layer = _layer_with_text_at((300, 1000, 780, 1200))
    cfg["text"]["color"] = [20, 20, 20]
    assert validate_mod.check_contrast(layer, background, cfg) != []


# ------------------------------------------------------------ music affinity


def test_nature_text_over_a_nature_track_is_silent(cfg):
    text = "The ocean and the river know the season; the forest waits for rain."
    assert validate_mod.check_music_affinity(text, "nature", cfg) == []


def test_nature_text_over_a_devotional_track_warns(cfg):
    text = "The ocean and the river know the season; the forest waits for rain."
    problems = validate_mod.check_music_affinity(text, "devotional", cfg)
    assert [p.check for p in problems] == ["music-affinity"]
    assert "nature" in problems[0].message


def test_neutral_text_never_warns(cfg):
    """No dominant signal means no opinion — this must not fire constantly."""
    text = "Your birth chart is your subconscious mind."
    for category in ("ambient", "nature", "devotional"):
        assert validate_mod.check_music_affinity(text, category, cfg) == []


def test_a_single_keyword_is_not_enough_to_warn(cfg):
    """One incidental word must not override the random pick."""
    text = "Let the rain be whatever it is."
    assert validate_mod.check_music_affinity(text, "devotional", cfg) == []


def test_the_affinity_check_can_be_switched_off(cfg):
    cfg["validation"]["music_affinity_check"] = False
    text = "The ocean and the river know the season; the forest waits for rain."
    assert validate_mod.check_music_affinity(text, "devotional", cfg) == []
