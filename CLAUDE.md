# divine-guidance — project rules

Short vertical reels (astrological / spiritual guidance) for Instagram Reels,
YouTube Shorts and TikTok. One fixed background image + overlaid text + a
randomly-picked royalty-free track.

## How a reel gets made

```bash
python3 src/make_reel.py "The text to overlay"
```

Everything else is automatic: duration from word count, random music, 1080x1920
H.264 output in `output/`.

## Hard rules

- **Music must be CC0.** Never add a track to `assets/music/` unless the licence
  is CC0 or an equivalent no-attribution commercial-use grant. Anything else
  risks Instagram/TikTok Content ID muting or removing the reel. `fetch_music.py`
  filters on `license=cc0` — do not loosen that filter.
- **Never commit credentials.** OAuth tokens live in `secrets/`, which is
  gitignored. Do not print refresh tokens to the terminal.
- **Duration must stay within 5–90s.** Instagram only surfaces videos in the
  Reels tab inside that window; outside it, the post silently becomes a normal
  video. The config clamps to 7–15s, well inside.
- **Keep 1080x1920 / 9:16.** All three platforms want the same shape, so one
  render serves all three. Do not add per-platform variants without a reason.

## Layout of the current background

The image is a diya on near-black, 768x593 landscape. It is composed with
`fit_mode: "contain"` rather than `cover`, deliberately:

- Cover would need a 6.5x upscale on a 10 KB source and would crop the bowl's
  sides off. Contain needs only 1.4x and keeps the whole lamp.
- The black padding is invisible against the photo's own black background.
- `image_anchor: 0.80` pins the photo flush to the frame bottom. Any lower value
  leaves the photo's bottom edge visible as a horizontal seam.
- `scrim_opacity: 0` — over an already-black image a scrim only lifts the blacks
  to grey and reads as a washed-out rectangle behind the text.

Text sits in the upper third (`vertical_anchor: 0.28`), the flame in the lower.
If the background image is ever swapped for a brighter or portrait one, revisit
all four of those settings — most likely back to `cover` with a scrim.

## Storage

Measured, not estimated:

- One reel ≈ **1.2 MB** (7–15s at CRF 20). ~15 MB/month at 3 reels/week.
- Music library: **146 MB, fixed** — it does not grow. 58 CC0 tracks.
- Retention: `output/` is pruned after every render — reels older than 60 days
  go, but the newest 10 are always kept. Tune under `retention` in `config.json`,
  or run `python3 src/prune.py --dry-run` to preview.

Pruning is safe: a deleted reel is re-creatable byte-for-byte from the same text
plus `--seed`. Nothing in `assets/` is ever touched.

## Working notes

- **Use absolute paths in Bash calls.** The shell's working directory persists
  between tool calls, so a stray `cd` earlier in a session silently breaks later
  relative paths. Prefix each command with a `cd` to the project root.
- **Add an import and its call site in the same edit.** A ruff format hook runs
  on write and strips imports that are not yet referenced, so adding the import
  first silently loses it and the next run dies with a `NameError`.
- `assets/music/manifest.json` is metadata only. Track selection scans the
  directory, so the library is whatever mp3s are on disk — an interrupted fetch
  or hand-dropped files both work without a rebuild.
- `fetch_music.py` is incremental: re-run it any time to top up the library.
  Existing files are skipped.
- Style: prefer `type` over `interface`, never `enum`, in any TS added later.

## Posting status

Platform APIs are all free, but each has an approval gate before public posts
are possible. Track progress in `POSTING.md`.
