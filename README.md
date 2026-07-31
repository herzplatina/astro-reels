# astro-reels

Turn a line of text into a short vertical video — fixed background image,
overlaid text, a randomly chosen royalty-free track — sized for Instagram Reels,
YouTube Shorts and TikTok at once.

Built for a channel that posts astrological and spiritual guidance a few times a
week, where the visual identity stays constant and only the words change.

```bash
python3 src/make_reel.py "Your birth chart is your subconscious mind."
```

```
  text     Your birth chart is your subconscious mind.
  duration 9.5s
  music    Tibetan singing bowl (ambient)
  output   output/your-birth-chart-is-your-subconscious-mi.mp4  [0.7 MB]
  captions output/your-birth-chart-is-your-subconscious-mi.caption.txt
```

Roughly fifteen seconds per render, about 1 MB out.

## What it does

- **Fits the text automatically.** Wraps to real rendered glyph widths and shrinks
  the font until the block fits the safe area — long and short lines both land.
- **Picks duration from word count**, clamped to 7–15s. Comfortably inside the
  5–90s window Instagram requires for a video to reach the Reels tab.
- **Chooses music at random**, from a random offset within the track, so two reels
  drawing the same file do not sound alike.
- **Writes per-platform captions** next to the video, with hashtag counts tuned
  per platform.
- **Prunes itself.** Reels older than 60 days are deleted after each render, but
  the newest 10 always survive.
- **Refuses to render a broken reel.** Pre-flight checks run on the composed
  frame before encoding starts.

## Pre-flight checks

The failure modes that matter here are all invisible until someone watches the
finished video, so they are measured from the rendered pixels and block the
render rather than warn after the fact.

| Check               | Fails when                                                                                                                         |
| ------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| `overflow`          | Glyphs fall outside the safe area — which shrinks to match the zoom, since text safe on the first frame can be cropped by the last |
| `subject-collision` | Text lands on the lit subject, found by luminance rather than hardcoded coordinates                                                |
| `contrast`          | WCAG ratio under the glyphs drops below 4.5:1                                                                                      |
| `music-affinity`    | _(warning only)_ The text leans strongly toward a category the chosen track is not from                                            |

```
This reel would not look right:
  ✗ overflow: text runs outside the safe area — 150px above the top edge
    → Shorten the text, or lower text.max_size / raise text.max_height_fraction.
```

`--force` renders anyway. On failure the intermediate frames stay in
`output/.build/` so you can see what tripped.

The music check is a keyword heuristic, not comprehension — it catches a line
about oceans landing on a sitar, and will miss subtler mismatches. It cannot
judge whether a track _sounds_ right, which is why it warns rather than blocks.

## Setup

Requires `ffmpeg` and Python 3.11+ with Pillow.

```bash
brew install ffmpeg
python3 -m pip install pillow
python3 src/fetch_music.py    # builds the CC0 music library, ~146 MB
```

Then drop a background image at `assets/image/` and point `background_image` in
`config.json` at it.

The default `text.font` is a macOS path. On Linux or Windows the tool falls back
to a bundled-serif search, but set `text.font` to something you actually like.

```bash
python3 -m pytest tests/ -q    # 25 tests, no ffmpeg needed
```

## Music licensing

`fetch_music.py` pulls **CC0 only**, via the Openverse API. This is deliberate
and should not be loosened: commercial tracks get muted or flagged by Content ID
on Instagram and TikTok, and CC0 carries no attribution burden. The mp3s are
gitignored — re-run the fetcher to rebuild the library.

## Configuration

Everything tunable lives in `config.json`: frame size, zoom, fit mode, font and
text metrics, duration rules, retention window, and the hashtag sets.

The current settings are tuned for a **dark landscape** background image
(contain-and-pad, no scrim, text in the upper third). Swapping in a brighter or
portrait image means revisiting them — see `CLAUDE.md` for which and why.

## Posting

Video generation is free and works today. Automated _posting_ is gated behind
platform approvals — all free, but each with a queue. `POSTING.md` has the
roadmap, including one trap worth knowing: **YouTube uploads from an unaudited
API project are permanently locked private, with no appeal.**

## Layout

```
src/make_reel.py     render a reel
src/fetch_music.py   build the CC0 music library
src/prune.py         age out old reels
config.json          all tunable settings
CLAUDE.md            project rules and design decisions
POSTING.md           platform API approval roadmap
```
