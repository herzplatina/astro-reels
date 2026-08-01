#!/usr/bin/env python3
"""Clients for the three publishing APIs.

Each platform ingests video differently, which is why only one of them needs the
GitHub Pages host:

    Instagram   fetches from a public HTTPS URL; no file upload exists
    YouTube     resumable upload of the bytes; never fetches a URL
    TikTok      chunked upload of the bytes. Its pull-from-URL mode would need
                DNS-record domain verification, impossible on github.io

Credentials live in secrets/credentials.json, which is gitignored. Nothing here
prints a token.

NOTE: these paths have not been exercised against the live APIs — that needs
approvals that are still in the queue. `--dry-run` is the tested path. Treat the
first real publish on each platform as a test, and expect to adjust.
"""

from __future__ import annotations

import json
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CREDENTIALS = ROOT / "secrets" / "credentials.json"

PLATFORMS = ("instagram", "youtube", "tiktok")


@dataclass
class PublishResult:
    ok: bool
    post_id: str = ""
    error: str = ""
    # True when retrying cannot possibly help: missing credentials, a rejected
    # token, a policy refusal. Distinguishing these stops the retry loop
    # hammering a wall forever and surfaces the problem to a human instead.
    permanent: bool = False


# HTTP statuses where the request itself was understood and refused. Retrying
# an identical request will be refused identically.
PERMANENT_STATUSES = frozenset({400, 401, 403, 404, 422})


class CredentialsMissing(RuntimeError):
    pass


# Only these are required. Checking "every key present is non-empty" would fail
# on optional settings like tiktok.privacy_level being left blank.
REQUIRED_CREDENTIALS = {
    "instagram": ("access_token", "ig_user_id"),
    "youtube": ("client_id", "client_secret", "refresh_token"),
    "tiktok": ("access_token",),
}


def load_credentials(platform: str) -> dict:
    if not CREDENTIALS.exists():
        raise CredentialsMissing(
            f"No credentials at {CREDENTIALS}.\n"
            "Copy secrets/credentials.example.json and fill it in."
        )
    try:
        data = json.loads(CREDENTIALS.read_text())
    except json.JSONDecodeError as exc:
        raise CredentialsMissing(f"{CREDENTIALS} is not valid JSON: {exc}") from exc

    section = data.get(platform) or {}
    missing = [k for k in REQUIRED_CREDENTIALS.get(platform, ()) if not section.get(k)]
    if missing:
        raise CredentialsMissing(
            f"{platform}: missing {', '.join(missing)} in {CREDENTIALS.name}."
        )
    return section


def _request(
    url: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict | None = None,
    timeout: int = 120,
) -> tuple[int, dict | bytes]:
    request = urllib.request.Request(
        url, data=data, method=method, headers=headers or {}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        body, status = exc.read(), exc.code
    try:
        return status, json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return status, body


def _form(payload: dict) -> bytes:
    return urllib.parse.urlencode(payload).encode()


def _json_body(payload: dict) -> bytes:
    return json.dumps(payload).encode()


# --------------------------------------------------------------- Instagram


def publish_instagram(
    video_url: str, caption: str, poll_seconds: int = 300
) -> PublishResult:
    """Two-step container flow: create, wait for processing, publish.

    Instagram accepts only a URL — it pulls the file itself, which is the entire
    reason the Pages host exists.
    """
    import time

    creds = load_credentials("instagram")
    token = creds["access_token"]
    user_id = creds["ig_user_id"]
    base = "https://graph.facebook.com/v21.0"

    status, body = _request(
        f"{base}/{user_id}/media",
        method="POST",
        data=_form(
            {
                "media_type": "REELS",
                "video_url": video_url,
                "caption": caption,
                "access_token": token,
            }
        ),
    )
    if status != 200 or "id" not in body:
        return PublishResult(
            False,
            error=f"container creation failed: {body}",
            permanent=status in PERMANENT_STATUSES,
        )
    container = body["id"]

    # Instagram downloads and transcodes before the container is publishable.
    deadline = time.time() + poll_seconds
    while time.time() < deadline:
        status, body = _request(
            f"{base}/{container}?fields=status_code,status&access_token={token}"
        )
        code = (body or {}).get("status_code")
        if code == "FINISHED":
            break
        if code == "ERROR":
            # Instagram rejected the media itself; the same file will be
            # rejected again.
            return PublishResult(
                False, error=f"container error: {body.get('status')}", permanent=True
            )
        time.sleep(5)
    else:
        return PublishResult(False, error="container did not finish in time")

    status, body = _request(
        f"{base}/{user_id}/media_publish",
        method="POST",
        data=_form({"creation_id": container, "access_token": token}),
    )
    if status != 200 or "id" not in body:
        return PublishResult(
            False,
            error=f"publish failed: {body}",
            permanent=status in PERMANENT_STATUSES,
        )
    return PublishResult(True, post_id=str(body["id"]))


# ----------------------------------------------------------------- YouTube


def _youtube_access_token(creds: dict) -> str:
    status, body = _request(
        "https://oauth2.googleapis.com/token",
        method="POST",
        data=_form(
            {
                "client_id": creds["client_id"],
                "client_secret": creds["client_secret"],
                "refresh_token": creds["refresh_token"],
                "grant_type": "refresh_token",
            }
        ),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if status != 200 or "access_token" not in body:
        raise RuntimeError(f"token refresh failed: {body}")
    return body["access_token"]


def publish_youtube(video: Path, title: str, description: str) -> PublishResult:
    """Resumable upload. YouTube takes the bytes directly and never fetches a URL.

    Uploads from an unaudited API project are permanently locked private with no
    appeal, so do not point this at a real account before the audit clears.
    """
    creds = load_credentials("youtube")
    token = _youtube_access_token(creds)

    metadata = {
        "snippet": {
            "title": title[:100],  # YouTube hard-caps titles at 100 characters
            "description": description[:5000],
            "categoryId": "22",
        },
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
    }
    size = video.stat().st_size

    # The session URL arrives as a Location header rather than in the body, so
    # this one call is made directly. Issuing it twice would open two upload
    # sessions and orphan one of them.
    request = urllib.request.Request(
        "https://www.googleapis.com/upload/youtube/v3/videos"
        "?uploadType=resumable&part=snippet,status",
        data=_json_body(metadata),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(size),
            "X-Upload-Content-Type": "video/mp4",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            upload_url = response.headers.get("Location")
    except urllib.error.HTTPError as exc:
        return PublishResult(False, error=f"resumable init failed: {exc.read()!r}")

    if not upload_url:
        return PublishResult(False, error="no upload URL returned")

    status, body = _request(
        upload_url,
        method="PUT",
        data=video.read_bytes(),
        headers={"Content-Type": "video/mp4", "Content-Length": str(size)},
        timeout=600,
    )
    if status not in (200, 201) or "id" not in (body or {}):
        return PublishResult(
            False,
            error=f"upload failed: {body}",
            permanent=status in PERMANENT_STATUSES,
        )

    # An unaudited project uploads successfully and gets a video ID back, but
    # YouTube silently locks the video private and the lock cannot be appealed.
    # Treating that as published would be a lie that releases the hosted file
    # for a video nobody can watch.
    granted = ((body.get("status") or {}).get("privacyStatus") or "").lower()
    if granted and granted != "public":
        return PublishResult(
            False,
            post_id=str(body["id"]),
            error=(
                f"uploaded as {granted!r}, not public — this is the unaudited-project "
                "lock, and it cannot be appealed. Complete the YouTube API audit, "
                "then re-upload by hand."
            ),
            permanent=True,
        )
    return PublishResult(True, post_id=str(body["id"]))


# ------------------------------------------------------------------ TikTok


def publish_tiktok(
    video: Path, caption: str, chunk_size: int = 10_000_000
) -> PublishResult:
    """Direct chunked upload.

    FILE_UPLOAD rather than PULL_FROM_URL on purpose: the pull mode requires
    proving domain ownership through a DNS record, which is impossible on a
    github.io subdomain.

    Until the app passes TikTok's audit, posts are forced to SELF_ONLY.
    """
    creds = load_credentials("tiktok")
    token = creds["access_token"]
    size = video.stat().st_size
    chunk_size = min(chunk_size, size)
    chunks = max(1, -(-size // chunk_size))

    status, body = _request(
        "https://open.tiktokapis.com/v2/post/publish/video/init/",
        method="POST",
        data=_json_body(
            {
                "post_info": {
                    "title": caption[:2200],
                    "privacy_level": creds.get("privacy_level", "SELF_ONLY"),
                },
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": size,
                    "chunk_size": chunk_size,
                    "total_chunk_count": chunks,
                },
            }
        ),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
    )
    data = (body or {}).get("data") or {}
    upload_url = data.get("upload_url")
    publish_id = data.get("publish_id")
    if status != 200 or not upload_url:
        return PublishResult(False, error=f"init failed: {body}")

    payload = video.read_bytes()
    for index in range(chunks):
        start = index * chunk_size
        end = size - 1 if index == chunks - 1 else start + chunk_size - 1
        piece = payload[start : end + 1]
        upload_status, upload_body = _request(
            upload_url,
            method="PUT",
            data=piece,
            headers={
                "Content-Type": mimetypes.guess_type(video.name)[0] or "video/mp4",
                "Content-Length": str(len(piece)),
                "Content-Range": f"bytes {start}-{end}/{size}",
            },
            timeout=600,
        )
        if upload_status not in (200, 201, 206):
            return PublishResult(
                False,
                error=f"chunk {index + 1}/{chunks} failed: {upload_body}",
                permanent=upload_status in PERMANENT_STATUSES,
            )

    # The upload finishing only means the bytes arrived. TikTok then processes
    # the video, and that can still fail. Reporting success here would let the
    # orchestrator delete the hosted file for a post that never went live.
    return _await_tiktok_publish(token, str(publish_id or ""))


def _await_tiktok_publish(
    token: str, publish_id: str, poll_seconds: int = 300
) -> PublishResult:
    import time

    if not publish_id:
        return PublishResult(False, error="no publish_id returned from init")

    deadline = time.time() + poll_seconds
    last = ""
    while time.time() < deadline:
        status, body = _request(
            "https://open.tiktokapis.com/v2/post/publish/status/fetch/",
            method="POST",
            data=_json_body({"publish_id": publish_id}),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
        )
        data = (body or {}).get("data") or {}
        last = data.get("status", "")
        if last in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"):
            return PublishResult(True, post_id=publish_id)
        if last == "FAILED":
            reason = data.get("fail_reason", body)
            return PublishResult(
                False, error=f"processing failed: {reason}", permanent=True
            )
        time.sleep(5)

    return PublishResult(
        False, error=f"still {last or 'unknown'} after {poll_seconds}s — check the app"
    )
