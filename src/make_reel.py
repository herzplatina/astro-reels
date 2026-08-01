#!/usr/bin/env python3
"""Render one short vertical reel: background image + overlaid text + music.

    python3 src/make_reel.py "Your guidance text here"
    python3 src/make_reel.py --text-file today.txt --duration 12

Output is a 1080x1920 H.264 mp4 in output/, sized and encoded to satisfy
Instagram Reels, YouTube Shorts and TikTok simultaneously.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import textwrap
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from media import probe_duration, require_tools
from prune import prune_from_config
from validate import validate


ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
MUSIC_MANIFEST = ROOT / "assets" / "music" / "manifest.json"
OUTPUT_DIR = ROOT / "output"
BUILD_DIR = ROOT / "output" / ".build"

# The window Instagram requires for a video to reach the Reels tab. Outside it
# the post still succeeds, but lands as an ordinary video.
PLATFORM_MIN_SECONDS = 5
PLATFORM_MAX_SECONDS = 90

# Intermediate frames are namespaced per process so two renders started at the
# same time cannot overwrite each other's text layer and emit a valid video
# carrying the wrong words.
RUN_ID = os.getpid()

# Tried in order when the configured font cannot be opened, so the tool still
# runs on a machine that is not the one it was configured on.
FONT_FALLBACKS = (
    "/System/Library/Fonts/Supplemental/Georgia.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSerif.ttf",
    "C:\\Windows\\Fonts\\georgia.ttf",
)


@dataclass
class Track:
    path: Path
    title: str
    category: str


def load_config() -> dict:
    cfg = json.loads(CONFIG_PATH.read_text())

    # Several of these are interpolated into an ffmpeg -filter_complex string.
    # Coercing them here means a value like "1,amovie=/etc/passwd" fails loudly
    # at load rather than becoming an extra filter in the graph.
    numeric = {
        "video": {"width": int, "height": int, "fps": int, "zoom_amount": float},
        "audio": {"fade_in_seconds": float, "fade_out_seconds": float},
        "duration": {
            "words_per_second": float,
            "buffer_seconds": float,
            "min_seconds": float,
            "max_seconds": float,
        },
    }
    for section, fields in numeric.items():
        for key, cast in fields.items():
            if key in cfg.get(section, {}):
                try:
                    cfg[section][key] = cast(cfg[section][key])
                except (TypeError, ValueError):
                    raise SystemExit(
                        f"config.json: {section}.{key} must be a number, "
                        f"got {cfg[section][key]!r}"
                    ) from None

    if cfg.get("duration", {}).get("words_per_second", 1) <= 0:
        raise SystemExit("config.json: duration.words_per_second must be > 0.")
    return cfg


# ---------------------------------------------------------------- duration


def compute_duration(text: str, cfg: dict, override: float | None) -> float:
    if override is not None:
        if override <= 0:
            raise SystemExit(f"--duration must be positive (got {override}).")
        if not (PLATFORM_MIN_SECONDS <= override <= PLATFORM_MAX_SECONDS):
            # Not fatal — you may deliberately want an off-spec render — but
            # outside this window Instagram quietly publishes the video as an
            # ordinary post instead of surfacing it in the Reels tab.
            print(
                f"  ! {override}s is outside the {PLATFORM_MIN_SECONDS}-"
                f"{PLATFORM_MAX_SECONDS}s window Instagram requires for Reels.",
                file=sys.stderr,
            )
        return float(override)

    d = cfg["duration"]
    words = len(text.split())
    seconds = words / d["words_per_second"] + d["buffer_seconds"]
    return round(max(d["min_seconds"], min(d["max_seconds"], seconds)), 2)


# ------------------------------------------------------------------- music


def pick_track(category: str | None = None) -> Track:
    """Pick a random track by scanning the library on disk.

    Disk is the source of truth rather than the manifest, so an interrupted
    fetch or hand-dropped mp3s both work without a rebuild step. The manifest
    is consulted only to put a human-readable title in the log.
    """
    music_root = MUSIC_MANIFEST.parent
    if not music_root.is_dir():
        raise SystemExit(
            f"No music directory at {music_root}. Run: python3 src/fetch_music.py"
        )

    if category:
        # Match against the categories that actually exist rather than joining
        # the argument onto a path, so '--music ../..' cannot escape the library.
        available = sorted(p.name for p in music_root.iterdir() if p.is_dir())
        if category not in available:
            raise SystemExit(
                f"Unknown music category {category!r}. "
                f"Available: {', '.join(available) or 'none'}"
            )
        search_root = music_root / category
    else:
        search_root = music_root

    files = sorted(search_root.rglob("*.mp3"))
    if not files:
        raise SystemExit(
            f"No tracks found in {search_root}. Run: python3 src/fetch_music.py"
        )

    titles: dict[str, str] = {}
    if MUSIC_MANIFEST.exists():
        try:
            titles = {
                Path(e["filename"]).stem: e["title"]
                for e in json.loads(MUSIC_MANIFEST.read_text())
            }
        except (json.JSONDecodeError, KeyError, TypeError):
            pass  # cosmetic only — a broken manifest must not block a render

    choice = random.choice(files)
    return Track(
        path=choice,
        title=titles.get(choice.stem, choice.stem),
        category=choice.parent.name,
    )


# -------------------------------------------------------------------- text


def load_font(cfg: dict, size: int) -> ImageFont.FreeTypeFont:
    text_cfg = cfg["text"]
    try:
        return ImageFont.truetype(
            text_cfg["font"], size, index=text_cfg.get("font_index", 0)
        )
    except OSError:
        pass

    for candidate in FONT_FALLBACKS:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue

    raise SystemExit(
        f"Could not open the configured font {text_cfg['font']!r}, and no "
        "fallback font was found on this system.\n"
        "Set 'text.font' in config.json to a .ttf or .ttc file that exists here."
    )


def wrap_to_width(
    text: str, font: ImageFont.FreeTypeFont, max_width: int, draw: ImageDraw.ImageDraw
) -> list[str]:
    """Greedy word wrap against real rendered widths, honouring explicit newlines."""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph.strip():
            lines.append("")
            continue
        words = paragraph.split()
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if draw.textlength(candidate, font=font) <= max_width or not current:
                current = candidate
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)
    return lines


def fit_text(
    text: str, cfg: dict, canvas: tuple[int, int]
) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """Shrink the font until the wrapped block fits the safe area."""
    text_cfg = cfg["text"]
    width, height = canvas
    max_width = int(width * text_cfg["width_fraction"])
    max_height = int(height * text_cfg.get("max_height_fraction", 0.62))

    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))

    for size in range(text_cfg["max_size"], text_cfg["min_size"] - 1, -2):
        font = load_font(cfg, size)
        lines = wrap_to_width(text, font, max_width, probe)
        line_height = size * text_cfg["line_spacing"]
        if len(lines) * line_height <= max_height:
            return font, lines

    # Even at the smallest permitted size the block does not fit. Render it
    # anyway rather than refusing, but say so loudly — the alternative is a reel
    # with words silently cropped off the frame, which is only discovered by
    # watching it back.
    font = load_font(cfg, text_cfg["min_size"])
    lines = wrap_to_width(text, font, max_width, probe)
    overflow = len(lines) * font.size * text_cfg["line_spacing"] - max_height
    print(
        f"  ! text is too long to fit: {len(lines)} lines overflow the safe area "
        f"by ~{overflow:.0f}px and will be cropped.\n"
        f"    Shorten it, or raise text.max_height_fraction in config.json.",
        file=sys.stderr,
    )
    return font, lines


def render_text_layer(text: str, cfg: dict) -> Path:
    """Draw the wrapped text (plus a legibility scrim) to a transparent PNG."""
    width = cfg["video"]["width"]
    height = cfg["video"]["height"]
    text_cfg = cfg["text"]

    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))

    # Soft vertical scrim so text stays readable regardless of the photo behind it.
    # Skipped entirely at opacity 0 — over an already-dark image a scrim only
    # lifts the blacks to grey, which reads as a washed-out rectangle.
    peak = text_cfg["scrim_opacity"]
    if peak > 0:
        scrim = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        scrim_draw = ImageDraw.Draw(scrim)
        for y in range(height):
            # Strongest through the middle band where the text sits, fading to clear.
            distance = abs(y / height - text_cfg["vertical_anchor"])
            alpha = int(peak * max(0.0, 1.0 - (distance / 0.42) ** 2))
            scrim_draw.line([(0, y), (width, y)], fill=(0, 0, 0, alpha))
        scrim = scrim.filter(ImageFilter.GaussianBlur(8))
        layer = Image.alpha_composite(layer, scrim)

    font, lines = fit_text(text, cfg, (width, height))
    line_height = font.size * text_cfg["line_spacing"]
    block_height = len(lines) * line_height
    start_y = height * text_cfg["vertical_anchor"] - block_height / 2

    # Shadow drawn on its own layer so it can be blurred without touching the glyphs.
    shadow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    text_draw = ImageDraw.Draw(layer)
    colour = tuple(text_cfg["color"]) + (255,)

    for i, line in enumerate(lines):
        y = start_y + i * line_height
        line_width = text_draw.textlength(line, font=font)
        x = (width - line_width) / 2
        shadow_draw.text(
            (x, y + 4), line, font=font, fill=(0, 0, 0, text_cfg["shadow_opacity"])
        )
        text_draw.text((x, y), line, font=font, fill=colour)

    shadow = shadow.filter(ImageFilter.GaussianBlur(12))
    layer = Image.alpha_composite(
        Image.alpha_composite(Image.new("RGBA", (width, height), (0, 0, 0, 0)), shadow),
        layer,
    )

    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    out = BUILD_DIR / f"text_layer_{RUN_ID}.png"
    layer.save(out)
    return out


# -------------------------------------------------------------- background


def prepare_background(cfg: dict) -> Path:
    """Fit the source image to the vertical frame, at 2x for zoom headroom.

    Two modes:

    - ``cover`` scales up and crops to fill. Right for an image that already has
      spare width, or whose edges do not matter.
    - ``contain`` scales to fit and pads the remainder. Right for a landscape
      image, a low-resolution one, or a subject on a flat background — padding
      in the matching colour is invisible, the whole subject survives, and the
      upscale factor stays far lower than cover would demand.
    """
    video = cfg["video"]
    src = ROOT / cfg["background_image"]
    if not src.exists():
        raise SystemExit(
            f"Background image not found at {src}.\n"
            "Drop your image there, or update 'background_image' in config.json."
        )

    width = video["width"] * 2
    height = video["height"] * 2
    mode = video.get("fit_mode", "cover")

    img = Image.open(src).convert("RGB")

    if mode == "contain":
        scale = min(width / img.width, height / img.height)
        new_size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
        resized = img.resize(new_size, Image.LANCZOS)

        canvas = Image.new(
            "RGB", (width, height), tuple(video.get("pad_color", [0, 0, 0]))
        )
        # Anchor vertically so the subject can sit low and leave the upper third
        # clear for text, rather than being locked to dead centre.
        centre_y = height * video.get("image_anchor", 0.5)
        top = round(centre_y - resized.height / 2)
        top = max(0, min(height - resized.height, top))
        canvas.paste(resized, ((width - resized.width) // 2, top))
        result = canvas
    else:
        scale = max(width / img.width, height / img.height)
        resized = img.resize(
            (
                max(width, round(img.width * scale)),
                max(height, round(img.height * scale)),
            ),
            Image.LANCZOS,
        )
        left = (resized.width - width) // 2
        top = (resized.height - height) // 2
        result = resized.crop((left, top, left + width, top + height))

    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    out = BUILD_DIR / f"background_{RUN_ID}.png"
    result.save(out)
    return out


# ----------------------------------------------------------------- caption


def write_captions(text: str, cfg: dict, out_path: Path) -> Path:
    """Write per-platform captions next to the video, ready to paste or post.

    Hashtag counts differ by platform on purpose: Instagram rewards a dozen,
    TikTok reads as spammy past a handful, and YouTube Shorts mainly needs
    #Shorts to be classified correctly.
    """
    tags = cfg["caption"]["hashtags"]
    caption_path = out_path.with_suffix(".caption.txt")

    sections = []
    for platform in ("instagram", "tiktok", "youtube"):
        hashtags = " ".join(tags.get(platform, []))
        sections.append(f"--- {platform.upper()} ---\n{text}\n\n{hashtags}\n")

    caption_path.write_text("\n".join(sections))
    return caption_path


# ------------------------------------------------------------------ render


def slugify(text: str, limit: int = 40) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return (slug[:limit].rstrip("-")) or "reel"


def _caption_text(caption: Path) -> str:
    """Recover the exact overlay text from a caption file written by write_captions."""
    body = caption.read_text()
    marker = "--- INSTAGRAM ---"
    if marker not in body:
        # A hand-written or older caption file: compare the whole body.
        return body.strip()
    section = body.split(marker, 1)[1]
    # The text runs up to the blank line that precedes the hashtags.
    return section.strip().split("\n\n", 1)[0].strip()


def unique_output_path(directory: Path, text: str) -> Path:
    """Pick an output path that will not silently destroy a different reel.

    The slug is truncated, so two different texts sharing an opening phrase map
    to the same name — and ffmpeg's -y would overwrite without asking. Re-running
    the *same* text still overwrites in place (that is intentional, so iterating
    on one reel does not litter the directory); a genuinely different text gets
    a numeric suffix instead.
    """
    base = slugify(text)
    candidate = directory / f"{base}.mp4"
    index = 2

    while candidate.exists():
        caption = candidate.with_suffix(".caption.txt")
        # Exact match, never a substring: one text being a prefix of another
        # collapses to the same truncated slug, and a substring test would then
        # hand back the other reel's path and overwrite it.
        if caption.exists() and _caption_text(caption) == text:
            return candidate
        candidate = directory / f"{base}-{index}.mp4"
        index += 1

    return candidate


def pick_start_offset(track: Path, reel_duration: float) -> float:
    """Choose a random window inside the track rather than always starting at 0:00.

    Most tracks run far longer than a reel, so seeking to a random point turns
    one file into many distinct-sounding beds. Without this, every reel that
    draws the same track sounds identical.
    """
    track_duration = probe_duration(track)
    slack = track_duration - reel_duration - 1.0
    if slack <= 0:
        return 0.0
    return round(random.uniform(0, slack), 2)


def render(
    text: str,
    cfg: dict,
    duration: float,
    track: Track,
    out_path: Path,
    force: bool = False,
) -> None:
    background = prepare_background(cfg)
    text_layer = render_text_layer(text, cfg)

    problems = validate(text_layer, background, cfg)
    if problems:
        report = "\n".join(problem.render() for problem in problems)
        if not force:
            # The intermediate frames are left in place deliberately — opening
            # output/.build/ shows exactly what tripped the check.
            raise SystemExit(
                f"This reel would not look right:\n{report}\n\n"
                f"Fix the above, or re-run with --force to render it anyway.\n"
                f"Inspect the frames at {BUILD_DIR}."
            )
        print(f"{report}\n  ! rendering anyway (--force).", file=sys.stderr)

    start_offset = pick_start_offset(track.path, duration)

    video = cfg["video"]
    audio = cfg["audio"]
    fps = video["fps"]
    total_frames = int(duration * fps)
    zoom = video["zoom_amount"]
    zoom_step = (zoom - 1.0) / max(total_frames - 1, 1)

    fade_out_start = max(duration - 1.0, 0.1)
    audio_fade_out_start = max(duration - audio["fade_out_seconds"], 0.1)

    filter_complex = (
        # Slow push-in on the still. Keeps the frame alive so the platforms don't
        # treat it as a static image, and reads as intentional rather than jittery.
        f"[0:v]zoompan=z='min(1+{zoom_step:.8f}*on,{zoom})'"
        f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":d={total_frames}:s={video['width']}x{video['height']}:fps={fps}[bg];"
        f"[1:v]format=rgba,"
        f"fade=in:st=0.3:d=0.9:alpha=1,"
        f"fade=out:st={fade_out_start:.2f}:d=0.9:alpha=1[txt];"
        f"[bg][txt]overlay=0:0:format=auto,format=yuv420p[v];"
        f"[2:a]afade=in:st=0:d={audio['fade_in_seconds']},"
        f"afade=out:st={audio_fade_out_start:.2f}:d={audio['fade_out_seconds']}[a]"
    )

    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-loop",
        "1",
        "-framerate",
        str(fps),
        "-i",
        str(background),
        "-loop",
        "1",
        "-framerate",
        str(fps),
        "-i",
        str(text_layer),
        "-stream_loop",
        "-1",
        "-ss",
        f"{start_offset}",
        "-i",
        str(track.path),
        "-filter_complex",
        filter_complex,
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-t",
        f"{duration}",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "20",
        "-profile:v",
        "high",
        "-level",
        "4.0",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "44100",
        "-movflags",
        "+faststart",
        str(out_path),
    ]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
        raise SystemExit("ffmpeg failed — see error above.")

    # Only on success: leaving them behind after a failure aids debugging.
    background.unlink(missing_ok=True)
    text_layer.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("text", nargs="?", help="The text to overlay on the reel.")
    parser.add_argument(
        "--text-file", type=Path, help="Read the text from a file instead."
    )
    parser.add_argument(
        "--duration", type=float, help="Override the computed duration."
    )
    parser.add_argument("--music", help="Restrict music to one category.")
    parser.add_argument(
        "--seed", type=int, help="Seed the music pick, for reproducible runs."
    )
    parser.add_argument("--out", type=Path, help="Output path for the mp4.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Render even if the pre-flight checks find problems.",
    )
    args = parser.parse_args()

    require_tools()

    if args.text_file:
        if not args.text_file.is_file():
            raise SystemExit(f"No such text file: {args.text_file}")
        text = args.text_file.read_text().strip()
    elif args.text:
        text = args.text.strip()
    else:
        parser.error("Provide text as an argument or via --text-file.")

    if not text:
        parser.error("The text is empty.")

    if args.seed is not None:
        random.seed(args.seed)

    cfg = load_config()
    duration = compute_duration(text, cfg, args.duration)
    track = pick_track(args.music)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = args.out or unique_output_path(OUTPUT_DIR, text)
    # ffmpeg will not create a missing parent, and its error for that names the
    # file rather than the directory, which reads as a permissions problem.
    out_path.parent.mkdir(parents=True, exist_ok=True)

    render(text, cfg, duration, track, out_path, force=args.force)
    caption_path = write_captions(text, cfg, out_path)

    size_mb = out_path.stat().st_size / 1_048_576
    preview = textwrap.shorten(text.replace("\n", " "), 60)
    print(f"  text     {preview}")
    print(f"  duration {duration}s")
    print(f"  music    {track.title} ({track.category})")
    print(f"  output   {out_path}  [{size_mb:.1f} MB]")
    print(f"  captions {caption_path}")

    pruned, freed = prune_from_config()
    if pruned:
        print(f"  pruned   {len(pruned)} old reels, {freed:.1f} MB reclaimed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
