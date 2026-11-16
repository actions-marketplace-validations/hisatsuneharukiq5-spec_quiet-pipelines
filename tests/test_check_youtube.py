import json

import pytest

from quiet_pipelines.checks import build
from tests.conftest import FakeResponse

TOKEN = json.dumps({"refresh_token": "r", "client_id": "c", "client_secret": "s", "token_uri": "https://oauth2.googleapis.com/token"})


def channel(name="My Channel"):
    return {"items": [{"id": "UC_x", "snippet": {"title": name}, "contentDetails": {"relatedPlaylists": {"uploads": "UU_x"}}}]}


def video(vid, published, privacy="public", publish_at=None):
    status = {"privacyStatus": privacy}
    if publish_at:
        status["publishAt"] = publish_at
    return {"id": vid, "snippet": {"publishedAt": published, "title": f"title {vid}"}, "status": status}


def wire(session, videos, chan=None, refresh=FakeResponse(200, {"access_token": "at"})):
    session.add("POST", "oauth2.googleapis.com/token", refresh)
    session.add("GET", "/youtube/v3/channels", FakeResponse(200, chan or channel()))
    session.add("GET", "/youtube/v3/playlistItems", FakeResponse(200, {"items": [{"contentDetails": {"videoId": v["id"]}} for v in videos]}))
    session.add("GET", "/youtube/v3/videos", FakeResponse(200, {"items": videos}))


def make(session, now, **params):
    spec = {"name": "yt", "type": "youtube_latest_upload", "auth": "oauth", "token_json_env": "YT_TOKEN", **params}
    return build(spec, session=session, now=now)


@pytest.fixture(autouse=True)
def token_env(monkeypatch):
    monkeypatch.setenv("YT_TOKEN", TOKEN)


def test_fresh_public_video_is_ok(session, now):
    wire(session, [video("a", "2026-11-16T02:00:00Z"), video("b", "2026-11-10T00:00:00Z")])
    r = make(session, now).run()
    assert r.ok and r.latest_at == "2026-11-16T02:00:00Z" and r.age_hours == 10.0
    assert r.details["latest_title"] == "title a"
    # access token from refresh is used; mine=true for oauth
    chan_call = next(c for c in session.calls if "/channels" in c["url"])
    assert chan_call["headers"]["Authorization"] == "Bearer at" and chan_call["params"]["mine"] == "true"


def test_stale_is_no_output(session, now):
    wire(session, [video("a", "2026-11-14T00:00:00Z")])
    r = make(session, now, max_age_hours=36).run()
    assert not r.ok and r.category == "no_output" and r.age_hours == 60.0


def test_scheduled_private_videos_do_not_count(session, now):
    wire(session, [video("s", "2026-11-16T11:00:00Z", privacy="private", publish_at="2026-11-17T00:00:00Z"),
                   video("a", "2026-11-13T00:00:00Z")])
    r = make(session, now).run()
    assert r.category == "no_output" and r.details["scheduled_not_public"] == 1


def test_no_public_videos(session, now):
    wire(session, [video("p", "2026-11-16T11:00:00Z", privacy="private")])
    r = make(session, now).run()
    assert r.category == "no_output" and "none of the last 1 uploads is public" in r.message


def test_refresh_invalid_grant_is_auth(session, now):
    wire(session, [], refresh=FakeResponse(400, {"error": "invalid_grant", "error_description": "Bad Request"}))
    r = make(session, now).run()
    assert r.category == "auth" and "invalid_grant" in r.message


def test_quota_exceeded_is_quota(session, now):
    session.add("POST", "oauth2.googleapis.com/token", FakeResponse(200, {"access_token": "at"}))
    session.add("GET", "/youtube/v3/channels", FakeResponse(403, {"error": {"code": 403, "errors": [{"reason": "quotaExceeded"}]}}))
    r = make(session, now).run()
    assert r.category == "quota" and "quotaExceeded" in r.message


def test_wrong_channel_is_auth(session, now):
    wire(session, [video("a", "2026-11-16T02:00:00Z")], chan=channel("Other Channel"))
    r = make(session, now, expect_channel_name_contains="My Channel").run()
    assert r.category == "auth" and "Other Channel" in r.message


def test_missing_env_is_config(session, now, monkeypatch):
    monkeypatch.delenv("YT_TOKEN")
    r = make(session, now).run()
    assert r.category == "config" and session.calls == []


def test_token_without_refresh_token_is_auth(session, now, monkeypatch):
    monkeypatch.setenv("YT_TOKEN", json.dumps({"token": "x"}))
    r = make(session, now).run()
    assert r.category == "auth"


def test_api_key_mode(session, now, monkeypatch):
    monkeypatch.setenv("YT_KEY", "k123")
    session.add("GET", "/youtube/v3/channels", FakeResponse(200, channel()), when=lambda c: c["params"].get("id") == "UC_x")
    session.add("GET", "/youtube/v3/playlistItems", FakeResponse(200, {"items": [{"contentDetails": {"videoId": "a"}}]}))
    session.add("GET", "/youtube/v3/videos", FakeResponse(200, {"items": [video("a", "2026-11-16T00:00:00Z")]}))
    r = build({"name": "yt", "type": "youtube_latest_upload", "auth": "api_key", "api_key_env": "YT_KEY", "channel_id": "UC_x"},
              session=session, now=now).run()
    assert r.ok
    assert all(c["params"].get("key") == "k123" for c in session.calls)
    assert not any(c["method"] == "POST" for c in session.calls)


def test_network_error_is_unreachable(session, now):
    import requests
    session.add("POST", "oauth2.googleapis.com/token", requests.ConnectionError("boom"))
    r = make(session, now).run()
    assert r.category == "unreachable"
