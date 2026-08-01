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


def test_the_text_mask_excludes_the_blurred_shadow(cfg):
    """Every other fixture uses alpha 0 or 255, so the threshold that separates
    glyphs from their shadow is otherwise untested — and lowering it to 1 went
    undetected."""
    from PIL import ImageFilter

    layer = Image.new("RGBA", FRAME, (0, 0, 0, 0))
    glyphs = (400, 900, 680, 1000)
    shadow = Image.new("RGBA", FRAME, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rectangle(glyphs, fill=(0, 0, 0, 150))
    layer = Image.alpha_composite(layer, shadow.filter(ImageFilter.GaussianBlur(30)))
    ImageDraw.Draw(layer).rectangle(glyphs, fill=(255, 255, 255, 255))

    bbox = validate_mod._text_mask(layer).getbbox()
    # The blur spreads well beyond the glyphs; the mask must not follow it.
    assert bbox[0] >= glyphs[0] - 2 and bbox[1] >= glyphs[1] - 2
    assert bbox[2] <= glyphs[2] + 2 and bbox[3] <= glyphs[3] + 2


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


def test_text_merely_crowding_the_subject_is_caught(cfg):
    """The clearance buffer is the point: text 12px from the flame reads as
    touching it. A test that only places text directly on top would pass with
    the dilation removed entirely."""
    background = _flat((0, 0, 0))
    ImageDraw.Draw(background).rectangle((300, 1420, 780, 1700), fill=(255, 200, 80))
    near = _layer_with_text_at((300, 1200, 780, 1408))  # 12px gap, no overlap

    from PIL import ImageChops

    subject = background.convert("L").point(lambda v: 255 if v > 30 else 0)
    raw = validate_mod._text_mask(near)
    assert ImageChops.multiply(subject, raw).histogram()[255] == 0, "must not overlap"

    problems = validate_mod.check_subject_collision(near, background, cfg)
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
    """The two behaviours must land on opposite sides of the threshold.

    Chosen so a whole-frame average would PASS (the frame is mostly black, so
    white text looks high-contrast) while the pixels actually under the glyphs
    are white and therefore illegible. A fixture where both readings fail proves
    nothing — the earlier version of this test passed even with the measurement
    replaced by a whole-frame average.
    """
    background = _flat((0, 0, 0))
    box = (300, 1000, 780, 1200)
    ImageDraw.Draw(background).rectangle(box, fill=(255, 255, 255))
    layer = _layer_with_text_at(box)
    cfg["text"]["color"] = [255, 255, 255]

    from PIL import ImageStat

    mask = validate_mod._text_mask(layer)
    under = tuple(ImageStat.Stat(background, mask).mean)
    whole = tuple(ImageStat.Stat(background).mean)
    assert validate_mod.contrast_ratio((255, 255, 255), under) < 4.5
    assert validate_mod.contrast_ratio((255, 255, 255), whole) > 4.5

    assert [p.check for p in validate_mod.check_contrast(layer, background, cfg)] == [
        "contrast"
    ]


def test_the_safe_margin_keeps_text_off_the_frame_edge(cfg):
    """Nothing pinned safe_margin_fraction, so ignoring it went undetected.

    Sits inside the zoom-visible region but inside the margin, so only the
    margin can reject it.
    """
    left, top, right, bottom = validate_mod.visible_region(FRAME, 1.06)
    fraction = cfg["validation"]["safe_margin_fraction"]
    margin_x = round(FRAME[0] * fraction)
    margin_y = round(FRAME[1] * fraction)  # derived from height, not width
    assert margin_x > 0 and margin_y > 0

    top_y = top + margin_y + 20
    breaching = _layer_with_text_at(
        (left + 2, top_y, right - margin_x - 20, top_y + 80)
    )
    problems = validate_mod.check_overflow(breaching, cfg, 1.06)
    assert [p.check for p in problems] == ["overflow"]
    assert "left" in problems[0].message

    clear = _layer_with_text_at(
        (left + margin_x + 5, top_y, right - margin_x - 20, top_y + 80)
    )
    assert validate_mod.check_overflow(clear, cfg, 1.06) == []
