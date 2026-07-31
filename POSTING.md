# Posting — how it works, and what is still gated

Sign in as the account owner for every registration below. The specific address
and handles live in `ACCOUNTS.local.md`, which is gitignored — personal
identifiers stay out of this public repo. Instagram must be a **Business**
account before any of this works.

Every API here is free. The cost is approval time, not money.

---

## The flow

```bash
python3 src/publish.py output/reel.mp4 --dry-run   # rehearse, no API calls
python3 src/publish.py output/reel.mp4             # host, publish, clean up
python3 src/publish.py --status                    # what is in flight
python3 src/publish.py --retry <slug>              # retry only what failed
python3 src/publish.py --sweep                     # release stranded hosts
```

1. The video is pushed to GitHub Pages and a public URL is returned.
2. All three platforms are published to, independently — one failing does not
   stop the others.
3. **The video is removed from hosting only once all three confirm.** Anything
   short of that keeps it hosted so the failures can be retried against the
   same URL.

State lives in `output/publish_state.json`, so an interrupted run resumes rather
than double-posting. A platform that already succeeded is never retried.

## Why only Instagram needs hosting

The three platforms ingest video differently:

| Platform  | Ingest                                                 | Needs the host? |
| --------- | ------------------------------------------------------ | --------------- |
| Instagram | Fetches from a public HTTPS URL. No upload API exists. | **Yes**         |
| YouTube   | Resumable upload of the bytes                          | No              |
| TikTok    | Chunked upload of the bytes                            | No              |

TikTok also offers `PULL_FROM_URL`, but that requires proving domain ownership
through a **DNS record** — impossible on a `github.io` subdomain, which we do
not control the DNS for. `FILE_UPLOAD` needs no verification, so that is the
route taken.

A broken Pages deploy therefore blocks Instagram only.

## Why GitHub Pages, and not the other GitHub options

All three were tested against a real mp4:

| Method                      | `content-type` served                                                                                    |
| --------------------------- | -------------------------------------------------------------------------------------------------------- |
| `raw.githubusercontent.com` | `application/octet-stream`                                                                               |
| Release assets              | `application/octet-stream`, plus `content-disposition: attachment` and a signed URL expiring in one hour |
| **GitHub Pages**            | **`video/mp4`**, with `accept-ranges: bytes`                                                             |

GitHub maps MIME types correctly for images only; everything else falls back to
octet-stream, and `x-content-type-options: nosniff` prevents a fetcher
recovering from it. Pages is a real static server and gets it right.

Hosting uses an **orphan `gh-pages` branch, force-pushed as a single amended
commit**. Committing videos to a normal branch would leave them in git history
permanently — bloating the repo forever and quietly contradicting the retention
policy, since history cannot be pruned the way `output/` can.

Capacity is not a constraint: the Pages limit is 1 GB (~830 reels) against a
~1.2 MB reel and a publish-gated cleanup that keeps only a handful live at once.
Bandwidth allowance is 100 GB/month; Instagram fetches each file once.

Edge caching is 10 minutes, so a deleted video can remain fetchable briefly.

---

## Approval status

### TikTok — drafts work today, audit for full auto

`video.upload` pushes the finished mp4 into your drafts; you tap publish. No
audit needed.

Fully automatic public posting needs `video.publish`, which requires an app
audit. Until it passes, posts are forced to `SELF_ONLY`, capped at 5 users per
24h, and the account must be private at post time. `privacy_level` in
`secrets/credentials.json` defaults to `SELF_ONLY` for that reason.

1. Register at developers.tiktok.com
2. Create an app, add the **Content Posting API** product
3. Request `video.upload` — granted quickly
4. Submit for audit requesting `video.publish`

### YouTube — audit required before anything is public

**Videos uploaded from an unaudited API project are permanently locked private,
with no appeal.** Do not point the uploader at a real account before the audit
clears; the only fix is re-uploading by hand.

1. Google Cloud console → new project → enable **YouTube Data API v3**
2. OAuth 2.0 credentials, application type **Desktop app**
3. Publish the consent screen to production — leaving it in Testing expires the
   refresh token every 7 days
4. Submit the **YouTube API Services audit**

Quota is 10,000 units/day, an upload costs ~1,600 → about 6 uploads a day. No
billing account, no card, and no paid tier exists.

### Instagram — Business account plus Meta app review

1. Convert the account to Business, link a Facebook Page
2. developers.facebook.com → create a Business app
3. Add the Instagram Graph API product
4. Submit App Review for `instagram_business_basic` and
   `instagram_business_content_publish` — separate submissions, each needing a
   screencast. Budget 2–4 weeks.

Rate limit: 25 published posts per 24h, shared with Stories.

---

## Credentials

Copy `secrets/credentials.example.json` to `secrets/credentials.json` and fill
it in. The whole `secrets/` directory is gitignored, and nothing in the code
prints a token.

## Status of this code

The hosting flow, the state machine and the publish-gated cleanup are tested and
working. **The three API clients have not been exercised against the live
APIs** — that needs approvals still in the queue. `--dry-run` is the tested
path. Treat the first real publish on each platform as a test.
