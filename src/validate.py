#!/usr/bin/env python3
"""Pre-flight checks on the composed frame, run before ffmpeg is invoked.

Every check here answers one question: would a viewer see something wrong?
They are measured against the actual rendered pixels rather than estimated from
line counts, because the failure modes they catch — a word cropped off the edge,
text sitting across the flame, grey-on-grey lettering — are all invisible until
someone watches the finished reel back.

Failures raise before any encoding happens, so a bad reel costs no render time.
`--force` renders anyway.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter, ImageStat


@dataclass
class Problem:
    check: str
    message: str
    hint: str

    def render(self) -> str:
        return f"  ✗ {self.check}: {self.message}\n    → {self.hint}"


# ------------------------------------------------------------------ helpers


def _relative_luminance(rgb: tuple[float, float, float]) -> float:
    """WCAG relative luminance, channels linearised from sRGB."""
    channels = []
    for value in rgb:
        c = value / 255.0
        channels.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = channels
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(
    foreground: tuple[float, float, float], background: tuple[float, float, float]
) -> float:
    """WCAG contrast ratio, 1.0 (identical) to 21.0 (black on white)."""
    lighter, darker = sorted(
        (_relative_luminance(foreground), _relative_luminance(background)), reverse=True
    )
    return (lighter + 0.05) / (darker + 0.05)


def visible_region(size: tuple[int, int], zoom: float) -> tuple[int, int, int, int]:
    """The area still on screen at maximum zoom.

    The push-in crops progressively, so anything outside this box is visible at
    the first frame and gone by the last. Checking against the full canvas would
    pass text that the viewer watches slide off the edge.
    """
    width, height = size
    if zoom <= 1.0:
        return (0, 0, width, height)
    inset_x = round(width * (1 - 1 / zoom) / 2)
    inset_y = round(height * (1 - 1 / zoom) / 2)
    return (inset_x, inset_y, width - inset_x, height - inset_y)


def _text_mask(text_layer: Image.Image) -> Image.Image:
    """Binary mask of the glyphs themselves, ignoring shadow and scrim.

    A high alpha cut is deliberate: the blurred drop shadow covers a much wider
    area than the letters, and treating it as text would fail every check.
    """
    alpha = text_layer.getchannel("A")
    return alpha.point(lambda a: 255 if a > 160 else 0)


# ------------------------------------------------------------------- checks


def check_overflow(text_layer: Image.Image, cfg: dict, zoom: float) -> list[Problem]:
    """Text must sit inside the safe area that survives the zoom."""
    validation = cfg["validation"]
    width, height = text_layer.size
    mask = _text_mask(text_layer)
    bbox = mask.getbbox()
    if bbox is None:
        return [
            Problem(
                "empty",
                "no text was rendered at all",
                "Check that the text argument is not blank.",
            )
        ]

    left, top, right, bottom = visible_region((width, height), zoom)
    margin_x = round(width * validation["safe_margin_fraction"])
    margin_y = round(height * validation["safe_margin_fraction"])
    safe = (left + margin_x, top + margin_y, right - margin_x, bottom - margin_y)

    problems = []
    tl, tt, tr, tb = bbox
    overruns = []
    if tl < safe[0]:
        overruns.append(f"{safe[0] - tl}px past the left edge")
    if tt < safe[1]:
        overruns.append(f"{safe[1] - tt}px above the top edge")
    if tr > safe[2]:
        overruns.append(f"{tr - safe[2]}px past the right edge")
    if tb > safe[3]:
        overruns.append(f"{tb - safe[3]}px below the bottom edge")

    if overruns:
        problems.append(
            Problem(
                "overflow",
                "text runs outside the safe area — " + ", ".join(overruns),
                "Shorten the text, or lower text.max_size / raise "
                "text.max_height_fraction in config.json.",
            )
        )
    return problems


def check_subject_collision(
    text_layer: Image.Image, background: Image.Image, cfg: dict
) -> list[Problem]:
    """Text must not land on the lit subject — the lamp, flame, or any bright area.

    Detected from the image itself rather than hardcoded coordinates, so this
    keeps working if the background is ever swapped.
    """
    validation = cfg["validation"]
    threshold = validation["subject_luminance_threshold"]
    clearance = validation["subject_clearance_px"]

    subject = background.convert("L").point(lambda v: 255 if v > threshold else 0)
    mask = _text_mask(text_layer)

    # Grow the text mask so text that merely crowds the subject also trips this,
    # not only text that literally overlaps a lit pixel.
    if clearance > 0:
        size = clearance * 2 + 1
        mask = mask.filter(ImageFilter.MaxFilter(min(size, 9)))
        for _ in range(max(0, clearance // 4 - 1)):
            mask = mask.filter(ImageFilter.MaxFilter(9))

    # Both are binary, so multiply is an intersection. Counting via the
    # histogram keeps this in C rather than iterating two million pixels.
    collision = ImageChops.multiply(subject, mask)
    overlap = collision.histogram()[255]

    tolerance = validation["subject_overlap_tolerance_px"]
    if overlap > tolerance:
        return [
            Problem(
                "subject-collision",
                f"text sits on the lit subject ({overlap} overlapping pixels, "
                f"tolerance {tolerance})",
                "Move the text block with text.vertical_anchor, or move the "
                "image with video.image_anchor, so they no longer share space.",
            )
        ]
    return []


def check_contrast(
    text_layer: Image.Image, background: Image.Image, cfg: dict
) -> list[Problem]:
    """Glyphs must be legible against whatever is actually behind them."""
    validation = cfg["validation"]
    minimum = validation["min_contrast_ratio"]

    mask = _text_mask(text_layer)
    if mask.getbbox() is None:
        return []

    # Average only the pixels the letters actually cover — a bright area
    # elsewhere in the frame must not rescue dark-on-dark text.
    mean_bg = tuple(ImageStat.Stat(background.convert("RGB"), mask).mean)
    text_colour = tuple(cfg["text"]["color"])

    ratio = contrast_ratio(text_colour, mean_bg)
    if ratio < minimum:
        return [
            Problem(
                "contrast",
                f"text contrast is {ratio:.1f}:1 against the background, "
                f"below the {minimum}:1 minimum",
                "Change text.color, raise text.scrim_opacity to darken what is "
                "behind the words, or move the text somewhere darker.",
            )
        ]
    return []


# ------------------------------------------------------------------- driver


def validate(text_layer_path: Path, background_path: Path, cfg: dict) -> list[Problem]:
    """Run every check on the composed frame. Returns the problems found.

    Only visual checks live here. Whether a track *suits* a line is a judgement
    call for whoever reviews the reel, not something to guess at in code.
    """
    if not cfg.get("validation", {}).get("enabled", True):
        return []

    text_layer = Image.open(text_layer_path).convert("RGBA")
    # The prepared background is 2x for zoom headroom; compare at frame scale.
    background = (
        Image.open(background_path)
        .convert("RGB")
        .resize(text_layer.size, Image.LANCZOS)
    )
    zoom = cfg["video"].get("zoom_amount", 1.0)

    problems: list[Problem] = []
    problems += check_overflow(text_layer, cfg, zoom)
    problems += check_subject_collision(text_layer, background, cfg)
    problems += check_contrast(text_layer, background, cfg)
    return problems
