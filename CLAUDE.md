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
- **Music must be pure instrumental, always.** No lyrics, no singing, and no
  spoken or incidental human voice of any kind. `fetch_music.py` screens tags
  before downloading and `audit_music.py` re-screens the whole library; both
  share the same term lists. Screening leans towards over-flagging on purpose —
  a wrongly quarantined track is one `--restore` away, whereas a reel that
  starts singing over the text is already published.
  Tags are human-written and therefore fallible, so
  `python3 src/audit_music.py --preview` builds one montage of every track for
  a listen-through. That is the only check that does not trust metadata.
- **Never commit credentials.** OAuth tokens live in `secrets/`, which is
  gitignored. Do not print refresh tokens to the terminal.
- **Duration must stay within 5–90s.** Instagram only surfaces videos in the
  Reels tab inside that window; outside it, the post silently becomes a normal
  video. The config clamps to 7–15s, well inside.
- **Keep 1080x1920 / 9:16.** All three platforms want the same shape, so one
  render serves all three. Do not add per-platform variants without a reason.

## Publishing

`src/publish.py` orchestrates; `src/hosting.py` handles GitHub Pages;
`src/platforms.py` holds the three API clients.

**Nothing is published without explicit human approval.** `confirm_approval()`
runs before the first side effect; a non-TTY run refuses unless `--yes` is
passed. Do not add a code path that reaches a platform API without it.

**The rule that governs everything: a video is removed from hosting only once
all three platforms have confirmed publication.** Anything short of that keeps
it hosted so the failures can be retried against the same URL. Do not "optimise"
this into cleaning up after a partial success.

- State is in `output/publish_state.json`. An interrupted run resumes; a
  platform that already succeeded is never retried.
- `--sweep` is a backstop only, for hosts stranded by a publish that never
  completed. It is not the primary cleanup path.

**Only genuine confirmation counts as success.** A false success releases the
hosted file, so each client verifies rather than assumes:

- TikTok's upload completing means the bytes arrived, not that the post went
  live — poll `publish/status/fetch` until `PUBLISH_COMPLETE`. Never treat the
  chunk upload as the end of the story.
- YouTube returns a video ID even when an unaudited project locks the video
  private, so check the returned `privacyStatus` and fail if it is not public.
- Instagram's container must reach `FINISHED` before publishing.

**`release_host()` must be reachable from every exit path**, including the one
where nothing is left to publish. A crash between the final publish and the
unpublish lands there on the next run, and an early return would strand the file
public with no route back.

Failures are classified `permanent` (refusals — credentials, 400/401/403,
policy) or transient. Permanent failures are never retried automatically;
transient ones stop at `MAX_ATTEMPTS`. An explicit `--retry` clears the block,
because that is a person saying they fixed the cause. `--abandon` is the escape
hatch from a permanently stuck reel and is the only thing that releases a host
without a successful publish.

- Only **Instagram** needs hosting — it exclusively fetches from a public URL.
  YouTube takes a resumable upload and TikTok a chunked upload. TikTok's
  `PULL_FROM_URL` would need DNS-record domain verification, impossible on
  `github.io`, so `FILE_UPLOAD` is used.

Hosting lives on an **orphan `gh-pages` branch, force-pushed as a single amended
commit**. Never commit videos to a normal branch: git history is permanent, so
they would bloat the repo forever and contradict the retention policy. Verify
with `git rev-list --count HEAD` in `.hosting/` — it must stay at 1.

Only GitHub Pages serves `video/mp4`. `raw.githubusercontent.com` and Release
assets both serve `application/octet-stream`, which with `nosniff` a fetcher
cannot recover from. Tested; do not switch hosts without re-testing headers.

The API clients are **untested against the live APIs** — approvals are pending.
`--dry-run` is the tested path.

## Pre-flight checks

`src/validate.py` runs on the composed frame before ffmpeg is invoked. All three
checks are errors that stop the render, measured from actual pixels rather than
estimated:

- **overflow** — glyph bounding box must sit inside the safe area. The safe area
  accounts for the zoom: at `zoom_amount` 1.06 the last frame shows only ~94% of
  the canvas, so text can be safe at t=0 and cropped by the end. Never check
  against the full canvas.
- **subject-collision** — the glyph mask, dilated by `subject_clearance_px`, must
  not intersect pixels brighter than `subject_luminance_threshold`. The subject
  is found from the image itself, so this keeps working if the artwork changes.
- **contrast** — WCAG ratio between `text.color` and the mean background
  _under the glyphs only_, against `min_contrast_ratio` (4.5:1). Averaging the
  whole frame would let a bright corner rescue unreadable text.

Whether a track _suits_ a line is deliberately **not** checked. That is a
judgement call for whoever reviews the reel; a keyword guess at it produces
noise and gets ignored. Do not reintroduce it.

`--force` renders despite errors. On failure the intermediate frames are left in
`output/.build/` on purpose — open them to see what tripped.

Use `ImageStat` / `ImageChops` / `histogram()` for pixel statistics, never
`getdata()` — it is deprecated in Pillow 14 and iterating 2M pixels in Python is
orders of magnitude slower.

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
- Music library: **146 MB, fixed** — it does not grow. 55 CC0 tracks
  (58 fetched, 3 quarantined for containing a human voice).
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

## Testing

`tests/conftest.py` replaces every hosting and network entry point with a
function that fails the test. This is not belt-and-braces: a fixture once
stubbed `hosting.publish`, the code was later changed to call `hosting.push`,
and the suite silently began pushing a fake video to the live Pages branch.
Stub what a test needs explicitly; never rely on the stub still covering the
path the code takes.

State on disk is written through a temp file and `os.replace`. A partial write
to `publish_state.json` would erase the record of what was already published,
and the next run would post it all again.

## Things a review has already caught once

Do not reintroduce these:

- **A dry run must never write state.** `save_state(state, dry_run)` returns
  early. Recording a rehearsal marked every platform published, and since that
  state is the only gate on whether a platform is attempted, the following real
  run posted nothing while printing success.
- **Screening splits on whitespace and punctuation.** Joining tag characters
  glued `"female vocal"` into one token that could never match, so multi-word
  vocal tags passed as instrumental.
- **`unique_output_path` compares text exactly, never as a substring.** Slugs
  truncate at 40 characters, so one text being a prefix of another collapsed to
  the same path and ffmpeg's `-y` destroyed the earlier reel.
- **A TikTok draft in the creator inbox is not published.** `SEND_TO_USER_INBOX`
  must not report success, or the host is released for a post nobody can see.
- **Publish runs hold an exclusive lock.** Two overlapping runs each read the
  state, each decided a platform was unpublished, and both posted it.
- **No credential in a URL.** Tokens go in an `Authorization` header; a token in
  a query string reaches exception text, the terminal, and `publish_state.json`.
- **Config values that reach the ffmpeg filtergraph are coerced numerically** at
  `load_config()`, and only `.mp4` may be handed to `hosting.push`.

## Test rigour

The suite is checked by mutation, not by count. Seed a defect into `src/`, run
the suite, and it must fail — 30 seeded defects, 30 caught. Re-run that check
after changing tests, because a test can go green for the wrong reason:

- Fixtures must **discriminate**. A contrast test where both the correct and
  the broken reading fail proves nothing; pick values that land on opposite
  sides of the threshold.
- Assert **which**, not just how many. "Keep the newest 3" and "keep the oldest
  3" produce identical counts.
- Never read a constant on both sides of an assertion — `MAX_ATTEMPTS == 5`
  pinned to a literal, or raising the constant goes undetected.
- A test named for a scenario must **create** that scenario. Two texts that
  "collide" must actually produce the same slug.
- Stubbing the unit under test tests the stub. `publish_one` is stubbed by every
  orchestration test, so its failure classification needs direct tests.

`tests/test_hosting.py` drives real git against a bare repo in a tmp dir, which
is why it carries the `allow_network` marker. It is the only thing that pins the
orphan/`--amend`/`gh-pages` invariants; without it, pointing `BRANCH` at `main`
passed every test.
