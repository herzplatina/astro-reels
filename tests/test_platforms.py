#!/usr/bin/env python3
"""Tests for the three publishing clients, driven through a scripted transport.

These exist because a mutation audit showed the clients were entirely untested:
removing the YouTube privacy check, reporting TikTok success before
PUBLISH_COMPLETE, treating a TikTok FAILED as success, publishing to Instagram
without waiting for FINISHED, and emptying PERMANENT_STATUSES all left the suite
green. Each of those is a rule that decides whether the hosted file is released,
so every one is pinned here.

`_request` is replaced with a scripted queue rather than a live call, so the
control flow is exercised without touching a network.

    python3 -m pytest tests/test_platforms.py -q
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import platforms as plat  # noqa: E402


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch):
    """The poll loops sleep 5s between attempts; tests must not."""
    monkeypatch.setattr(time, "sleep", lambda seconds: None)


@pytest.fixture
def credentials(tmp_path, monkeypatch):
    path = tmp_path / "credentials.json"
    path.write_text(
        json.dumps(
            {
                "instagram": {"access_token": "IG_TOKEN", "ig_user_id": "17841400000"},
                "youtube": {
                    "client_id": "cid",
                    "client_secret": "secret",
                    "refresh_token": "refresh",
                },
                "tiktok": {"access_token": "TT_TOKEN", "privacy_level": "SELF_ONLY"},
            }
        )
    )
    path.chmod(0o600)
    monkeypatch.setattr(plat, "CREDENTIALS", path)
    return path


def script(monkeypatch, *responses):
    """Queue up (status, body) pairs and record every outgoing request."""
    queue = list(responses)
    calls: list[dict] = []

    def fake_request(url, *, method="GET", data=None, headers=None, timeout=120):
        calls.append(
            {"url": url, "method": method, "data": data, "headers": headers or {}}
        )
        if not queue:
            raise AssertionError(f"unscripted request to {url}")
        return queue.pop(0)

    monkeypatch.setattr(plat, "_request", fake_request)
    return calls


# --------------------------------------------------------------- credentials


def test_missing_required_key_is_reported(credentials, monkeypatch):
    credentials.write_text(json.dumps({"instagram": {"access_token": "x"}}))
    with pytest.raises(plat.CredentialsMissing) as caught:
        plat.load_credentials("instagram")
    assert "ig_user_id" in str(caught.value)


def test_an_empty_required_value_counts_as_missing(credentials):
    credentials.write_text(
        json.dumps({"instagram": {"access_token": "x", "ig_user_id": ""}})
    )
    with pytest.raises(plat.CredentialsMissing):
        plat.load_credentials("instagram")


def test_an_optional_blank_value_is_allowed(credentials):
    """privacy_level may be blank; only the listed keys are required."""
    credentials.write_text(
        json.dumps({"tiktok": {"access_token": "x", "privacy_level": ""}})
    )
    assert plat.load_credentials("tiktok")["access_token"] == "x"


def test_a_corrupt_credentials_file_is_reported_clearly(credentials):
    credentials.write_text("{ not json")
    with pytest.raises(plat.CredentialsMissing) as caught:
        plat.load_credentials("tiktok")
    assert "not valid JSON" in str(caught.value)


def test_world_readable_credentials_warn(credentials, capsys):
    credentials.chmod(0o644)
    plat.load_credentials("tiktok")
    assert "chmod 600" in capsys.readouterr().err


# ----------------------------------------------------------------- Instagram


def test_instagram_waits_for_finished_before_publishing(credentials, monkeypatch):
    """Publishing an unfinished container is the failure this ordering prevents."""
    calls = script(
        monkeypatch,
        (200, {"id": "CONTAINER"}),
        (200, {"status_code": "IN_PROGRESS"}),
        (200, {"status_code": "IN_PROGRESS"}),
        (200, {"status_code": "FINISHED"}),
        (200, {"id": "POST_ID"}),
    )
    result = plat.publish_instagram("https://host/r.mp4", "caption")

    assert result.ok and result.post_id == "POST_ID"
    # The publish call must be last, i.e. after every poll.
    assert "media_publish" in calls[-1]["url"]
    assert sum("media_publish" in c["url"] for c in calls) == 1


def test_instagram_container_error_is_permanent(credentials, monkeypatch):
    script(
        monkeypatch,
        (200, {"id": "CONTAINER"}),
        (200, {"status_code": "ERROR", "status": "media rejected"}),
    )
    result = plat.publish_instagram("https://host/r.mp4", "caption")
    assert not result.ok and result.permanent


def test_instagram_never_publishes_after_a_container_error(credentials, monkeypatch):
    calls = script(
        monkeypatch,
        (200, {"id": "CONTAINER"}),
        (200, {"status_code": "ERROR", "status": "bad"}),
    )
    plat.publish_instagram("https://host/r.mp4", "caption")
    assert not any("media_publish" in c["url"] for c in calls)


def test_instagram_gives_up_if_the_container_never_finishes(credentials, monkeypatch):
    responses = [(200, {"id": "CONTAINER"})] + [
        (200, {"status_code": "IN_PROGRESS"}) for _ in range(500)
    ]
    calls = script(monkeypatch, *responses)
    result = plat.publish_instagram("https://host/r.mp4", "caption", poll_seconds=0)
    assert not result.ok
    assert not any("media_publish" in c["url"] for c in calls)


@pytest.mark.parametrize("status", sorted(plat.PERMANENT_STATUSES))
def test_instagram_refusals_are_permanent(credentials, monkeypatch, status):
    script(monkeypatch, (status, {"error": "refused"}))
    result = plat.publish_instagram("https://host/r.mp4", "caption")
    assert not result.ok and result.permanent


def test_instagram_server_error_is_transient(credentials, monkeypatch):
    script(monkeypatch, (500, {"error": "upstream"}))
    result = plat.publish_instagram("https://host/r.mp4", "caption")
    assert not result.ok and not result.permanent


def test_instagram_token_is_never_placed_in_a_url(credentials, monkeypatch):
    """A token in a query string reaches exception text, logs and state files."""
    calls = script(
        monkeypatch,
        (200, {"id": "CONTAINER"}),
        (200, {"status_code": "FINISHED"}),
        (200, {"id": "POST_ID"}),
    )
    plat.publish_instagram("https://host/r.mp4", "caption")
    for call in calls:
        assert "IG_TOKEN" not in call["url"]


# ------------------------------------------------------------------- YouTube


def _stub_resumable(monkeypatch, location="https://upload.example/session"):
    class FakeResponse:
        headers = {"Location": location}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        plat.urllib.request, "urlopen", lambda request, timeout=120: FakeResponse()
    )


def test_youtube_private_lock_is_a_failure_not_a_success(
    credentials, monkeypatch, tmp_path
):
    """An unaudited project returns 200 and a video ID while locking the video
    private. Calling that published releases the host for an unwatchable video."""
    video = tmp_path / "r.mp4"
    video.write_bytes(b"video")
    _stub_resumable(monkeypatch)
    script(
        monkeypatch,
        (200, {"access_token": "AT"}),
        (200, {"id": "VID", "status": {"privacyStatus": "private"}}),
    )

    result = plat.publish_youtube(video, "title", "description")
    assert not result.ok
    assert result.permanent
    assert "audit" in result.error.lower()


def test_youtube_public_upload_succeeds(credentials, monkeypatch, tmp_path):
    video = tmp_path / "r.mp4"
    video.write_bytes(b"video")
    _stub_resumable(monkeypatch)
    script(
        monkeypatch,
        (200, {"access_token": "AT"}),
        (200, {"id": "VID", "status": {"privacyStatus": "public"}}),
    )
    result = plat.publish_youtube(video, "title", "description")
    assert result.ok and result.post_id == "VID"


def test_youtube_refused_refresh_token_is_not_retryable(
    credentials, monkeypatch, tmp_path
):
    video = tmp_path / "r.mp4"
    video.write_bytes(b"video")
    script(monkeypatch, (400, {"error": "invalid_grant"}))
    with pytest.raises(plat.CredentialsMissing):
        plat.publish_youtube(video, "title", "description")


def test_youtube_title_is_capped_at_the_api_limit(credentials, monkeypatch, tmp_path):
    video = tmp_path / "r.mp4"
    video.write_bytes(b"video")
    captured = {}

    class FakeResponse:
        headers = {"Location": "https://upload.example/session"}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def capture(request, timeout=120):
        captured["body"] = json.loads(request.data)
        return FakeResponse()

    monkeypatch.setattr(plat.urllib.request, "urlopen", capture)
    script(
        monkeypatch,
        (200, {"access_token": "AT"}),
        (200, {"id": "VID", "status": {"privacyStatus": "public"}}),
    )
    plat.publish_youtube(video, "T" * 300, "D" * 9000)

    assert len(captured["body"]["snippet"]["title"]) == 100
    assert len(captured["body"]["snippet"]["description"]) == 5000


def test_youtube_missing_upload_url_fails(credentials, monkeypatch, tmp_path):
    video = tmp_path / "r.mp4"
    video.write_bytes(b"video")
    _stub_resumable(monkeypatch, location=None)
    script(monkeypatch, (200, {"access_token": "AT"}))
    result = plat.publish_youtube(video, "title", "description")
    assert not result.ok


# -------------------------------------------------------------------- TikTok


def _tiktok_script(monkeypatch, *status_bodies, upload_status=200):
    return script(
        monkeypatch,
        (200, {"data": {"upload_url": "https://upload.tiktok/x", "publish_id": "PID"}}),
        (upload_status, {}),
        *status_bodies,
    )


def test_tiktok_waits_for_publish_complete(credentials, monkeypatch, tmp_path):
    """The upload finishing only means the bytes arrived."""
    video = tmp_path / "r.mp4"
    video.write_bytes(b"x" * 100)
    _tiktok_script(
        monkeypatch,
        (200, {"data": {"status": "PROCESSING_UPLOAD"}}),
        (200, {"data": {"status": "PUBLISH_COMPLETE"}}),
    )
    result = plat.publish_tiktok(video, "caption")
    assert result.ok and result.post_id == "PID"


def test_tiktok_processing_failure_is_caught(credentials, monkeypatch, tmp_path):
    video = tmp_path / "r.mp4"
    video.write_bytes(b"x" * 100)
    _tiktok_script(
        monkeypatch, (200, {"data": {"status": "FAILED", "fail_reason": "too short"}})
    )
    result = plat.publish_tiktok(video, "caption")
    assert not result.ok and result.permanent
    assert "too short" in result.error


def test_a_tiktok_draft_is_not_a_published_post(credentials, monkeypatch, tmp_path):
    """SEND_TO_USER_INBOX means it is sitting unposted in the creator's drafts;
    reporting success would release the hosted file for an invisible post."""
    video = tmp_path / "r.mp4"
    video.write_bytes(b"x" * 100)
    _tiktok_script(monkeypatch, (200, {"data": {"status": "SEND_TO_USER_INBOX"}}))
    result = plat.publish_tiktok(video, "caption")
    assert not result.ok
    assert "draft" in result.error.lower()


def test_tiktok_status_timeout_is_not_success(credentials, monkeypatch, tmp_path):
    video = tmp_path / "r.mp4"
    video.write_bytes(b"x" * 100)
    _tiktok_script(
        monkeypatch, *[(200, {"data": {"status": "PROCESSING_UPLOAD"}})] * 500
    )
    result = plat._await_tiktok_publish("TOKEN", "PID", poll_seconds=0)
    assert not result.ok


def test_tiktok_chunk_upload_failure_stops_the_publish(
    credentials, monkeypatch, tmp_path
):
    video = tmp_path / "r.mp4"
    video.write_bytes(b"x" * 100)
    calls = _tiktok_script(monkeypatch, upload_status=403)
    result = plat.publish_tiktok(video, "caption")
    assert not result.ok and result.permanent
    # No status poll should follow a failed upload.
    assert not any("status/fetch" in c["url"] for c in calls)


def test_tiktok_defaults_to_self_only(credentials, monkeypatch, tmp_path):
    video = tmp_path / "r.mp4"
    video.write_bytes(b"x" * 100)
    calls = _tiktok_script(monkeypatch, (200, {"data": {"status": "PUBLISH_COMPLETE"}}))
    plat.publish_tiktok(video, "caption")
    sent = json.loads(calls[0]["data"])
    assert sent["post_info"]["privacy_level"] == "SELF_ONLY"


def test_tiktok_sends_the_whole_file(credentials, monkeypatch, tmp_path):
    """Every byte must be uploaded exactly once, with a correct Content-Range."""
    video = tmp_path / "r.mp4"
    payload = bytes(range(256)) * 40  # 10240 bytes
    video.write_bytes(payload)

    calls = script(
        monkeypatch,
        (200, {"data": {"upload_url": "https://upload.tiktok/x", "publish_id": "PID"}}),
        *[(200, {})] * 8,
        (200, {"data": {"status": "PUBLISH_COMPLETE"}}),
    )
    result = plat.publish_tiktok(video, "caption", chunk_size=4096)
    assert result.ok

    uploads = [c for c in calls if "upload.tiktok" in c["url"]]
    assert b"".join(c["data"] for c in uploads) == payload
    for call in uploads:
        assert call["headers"]["Content-Range"].endswith(f"/{len(payload)}")


# ------------------------------------------------------- non-JSON responses


def test_a_non_json_body_does_not_crash_the_caller(credentials, monkeypatch):
    """An HTML error page from a proxy used to reach callers as bytes and raise
    AttributeError, which was then misreported as a transient failure."""
    script(monkeypatch, (502, b"<html>bad gateway</html>"))
    monkeypatch.setattr(
        plat,
        "_request",
        lambda *a, **k: (502, {"_raw": "<html>bad gateway</html>"}),
    )
    result = plat.publish_instagram("https://host/r.mp4", "caption")
    assert not result.ok and not result.permanent
