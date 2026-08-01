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

```
This reel would not look right:
  ✗ overflow: text runs outside the safe area — 150px above the top edge
    → Shorten the text, or lower text.max_size / raise text.max_height_fraction.
```

`--force` renders anyway. On failure the intermediate frames stay in
`output/.build/` so you can see what tripped.

Whether a track _suits_ a line is deliberately not checked — that is a judgement
for whoever reviews the reel.

## Instrumental only

Background music must be pure instrumental: no lyrics, no singing, and no spoken
or incidental human voice. `fetch_music.py` screens Openverse tags before
downloading, and `audit_music.py` re-screens the whole library.

```bash
python3 src/audit_music.py --dry-run           # report only
python3 src/audit_music.py                     # quarantine anything with a voice
python3 src/audit_music.py --restore <id>      # walk back a false positive
```

Flagged tracks move to `assets/music_quarantine/`, never deleted. Screening
leans towards over-flagging on purpose: a wrongly quarantined track is one
restore away, whereas a reel that starts singing is already published.

Tags are human-written and therefore fallible, so there is one check that
trusts no metadata at all — a montage of every track in the library, with a
printed index, to listen through in a few minutes:

```bash
python3 src/audit_music.py --preview
```

## Publishing

```bash
python3 src/publish.py output/reel.mp4 --dry-run   # rehearse, no API calls
python3 src/publish.py output/reel.mp4             # host, publish, clean up
python3 src/publish.py --status                    # what is in flight
python3 src/publish.py --retry <slug>              # retry only what failed
python3 src/publish.py --abandon <slug>            # give up, release the host
```

The video is pushed to GitHub Pages, published to all three platforms
independently, and **removed from hosting only once all three confirm**. A
partial success keeps it hosted so the failures can be retried against the same
URL. State in `output/publish_state.json` means an interrupted run resumes
instead of double-posting.

Only genuine confirmation counts. TikTok's upload completing means the bytes
arrived, not that the post went live, so its publish status is polled; YouTube
returns a video ID even when an unaudited project locks the video private, so
the granted privacy is checked. A false success would release the hosted file.

Refusals — missing credentials, `401`, a policy rejection — are marked permanent
and never retried automatically. Transient failures retry up to five times. A
reel that runs out of road stays hosted and says so, and `--status` surfaces it;
fix the cause and `--retry`, or `--abandon` to release the file and move on.

Only Instagram needs the hosting — it exclusively fetches from a public URL,
whereas YouTube and TikTok take the bytes directly.

| Platform  | Ingest                     | Needs hosting? |
| --------- | -------------------------- | -------------- |
| Instagram | Fetches a public HTTPS URL | **Yes**        |
| YouTube   | Resumable upload           | No             |
| TikTok    | Chunked upload             | No             |

Hosting uses an orphan `gh-pages` branch force-pushed as a single amended
commit, so videos never enter git history — where they would live permanently
and make the retention policy meaningless.

Of the GitHub options, only Pages serves a correct `video/mp4`;
`raw.githubusercontent.com` and Release assets both serve
`application/octet-stream`. See `POSTING.md` for the measurements and for the
approval status of each platform.

Automated posting is gated behind platform approvals — all free, each with a
queue. The API clients are written but **not yet exercised against the live
APIs**; `--dry-run` is the tested path.

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
python3 -m pytest tests/ -q    # 116 tests, no ffmpeg or network needed
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

## Cost

Zero. ffmpeg, Python and the Openverse API are free; the music is CC0; GitHub
Pages is free for public repos; and all three platform APIs have free tiers with
no billing account attached. YouTube has no paid tier at all — its 10,000
units/day is a rate limit, not a bill, and an upload costs ~1,600 units.

One trap worth knowing: **YouTube uploads from an unaudited API project are
permanently locked private, with no appeal.** See `POSTING.md`.

## Layout

```
src/make_reel.py     render a reel
src/validate.py      pre-flight checks on the composed frame
src/fetch_music.py   build the CC0 music library
src/audit_music.py   screen the library for human voices
src/hosting.py       publish/unpublish on GitHub Pages
src/platforms.py     Instagram, YouTube and TikTok clients
src/publish.py       orchestrate publishing and publish-gated cleanup
src/prune.py         age out old reels
src/media.py         shared ffmpeg helpers
config.json          all tunable settings
CLAUDE.md            project rules and design decisions
POSTING.md           hosting rationale and platform approval status
```
