# Posting — approval roadmap

Sign in as the account owner for every developer-app registration and OAuth
consent below. The specific address and handles live in `ACCOUNTS.local.md`,
which is gitignored — personal identifiers stay out of this public repo.
Instagram must be a **Business** account before any of this works.

Every API below is free. The cost is approval time, not money. Nothing here
requires a paid tier, and the only recurring spend is $0.

Work top to bottom: TikTok drafts works today, the other two need applications
started now because they take weeks.

---

## TikTok — works today (drafts), audit for full auto

**Immediately usable.** The `video.upload` scope pushes the finished mp4 into
your TikTok drafts. You open the app and tap publish. No audit needed.

**For fully automatic public posting** you need the `video.publish` scope, which
requires an app audit. Until that passes:

- posts are forced to `SELF_ONLY` (private)
- max 5 users posting per 24h
- the account itself must be private at post time

Setup:

1. Create an account at developers.tiktok.com
2. Create an app, add the **Content Posting API** product
3. Request `video.upload` (granted quickly) — this unblocks the drafts flow
4. Submit for audit requesting `video.publish` for the public flow

## YouTube — audit required before anything is public

**Critical:** videos uploaded from an unaudited API project are **permanently
locked private, with no appeal**. You cannot unlock them; you would have to
re-upload by hand. So do not bulk-upload before the audit clears.

Setup:

1. Google Cloud console → new project → enable **YouTube Data API v3**
2. Create OAuth 2.0 credentials, application type **Desktop app**
3. Configure the OAuth consent screen and **publish it to production** — leaving
   it in Testing mode expires the refresh token every 7 days, which means
   re-authenticating constantly
4. Submit the **YouTube API Services audit** form to lift the private lock

Quota: 10,000 units/day free. An upload costs ~1,600 units, so roughly 6 uploads
per day. Far beyond 2–3 per week.

## Instagram — Business account + Meta app review

Longest lead time, and the one with a prerequisite you may need to fix first.

**Prerequisite:** the account must be a **Business** account — Creator accounts
cannot publish Reels through the API — and it must be linked to a Facebook Page.

Setup:

1. Convert the Instagram account to Business, link a Facebook Page
2. developers.facebook.com → create an app (Business type)
3. Add the Instagram Graph API product
4. Submit App Review for `instagram_business_basic` and
   `instagram_business_content_publish` — each needs its own submission with a
   screencast of the full flow. Budget 2–4 weeks.

**Extra wrinkle:** Instagram does not accept a file upload. It fetches the video
from a public HTTPS URL, so each reel has to be hosted somewhere public for a
few minutes. Free options that comfortably cover 2–3 videos a week at ~1 MB each:

- Cloudflare R2 — 10 GB free
- A public GitHub repo — raw URLs, files under 100 MB
- Backblaze B2 — 10 GB free

Rate limit: 25 published posts per 24h, with Reels and Stories sharing the bucket.

---

## Order of operations

1. **Now:** start the Meta app review and the YouTube audit — they are the long
   poles and both are queue-bound.
2. **Now:** request TikTok `video.upload` so the drafts flow works this week.
3. **Meanwhile:** post by hand from the rendered mp4. `make_reel.py` already
   writes per-platform captions alongside the video.
4. **As each approval lands:** flip that platform to automatic.
