import json

import pytest

from quiet_pipelines.checks import REGISTRY, BaseCheck, build, register
from quiet_pipelines.checks.feeds import parse_any_date
from tests.conftest import FakeResponse

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>old</title><pubDate>Mon, 10 Nov 2026 08:00:00 GMT</pubDate></item>
<item><title>new</title><pubDate>Sun, 16 Nov 2026 06:00:00 +0000</pubDate></item>
</channel></rss>"""
ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>a</title><updated>2026-11-01T00:00:00Z</updated></entry></feed>"""


def run(spec, session, now):
    return build(spec, session=session, now=now).run()


# --- rss_freshness ---------------------------------------------------------
def test_rss_newest_item(session, now):
    session.add("GET", "example.com/feed", FakeResponse(200, RSS))
    r = run({"name": "f", "type": "rss_freshness", "url": "https://example.com/feed"}, session, now)
    assert r.ok and r.latest_at == "2026-11-16T06:00:00Z" and r.details == {"items": 2, "latest_title": "new"}


def test_atom_stale(session, now):
    session.add("GET", "example.com/atom", FakeResponse(200, ATOM))
    r = run({"name": "f", "type": "rss_freshness", "url": "https://example.com/atom", "max_age_hours": 72}, session, now)
    assert r.category == "no_output"


def test_rss_invalid_xml_and_empty(session, now):
    session.add("GET", "/bad", FakeResponse(200, "<rss><oops"))
    session.add("GET", "/empty", FakeResponse(200, "<rss><channel></channel></rss>"))
    assert run({"name": "f", "type": "rss_freshness", "url": "https://x/bad"}, session, now).category == "api_error"
    assert run({"name": "f", "type": "rss_freshness", "url": "https://x/empty"}, session, now).category == "no_output"


def test_rss_missing_url_is_config(session, now):
    assert run({"name": "f", "type": "rss_freshness"}, session, now).category == "config"


def test_server_error_is_unreachable(session, now):
    session.add("GET", "/down", FakeResponse(503, "busy"))
    assert run({"name": "f", "type": "rss_freshness", "url": "https://x/down"}, session, now).category == "unreachable"


def test_headers_env(session, now, monkeypatch):
    monkeypatch.setenv("FEED_HEADERS", json.dumps({"Authorization": "Bearer t"}))
    session.add("GET", "/private", FakeResponse(200, RSS), when=lambda c: c["headers"].get("Authorization") == "Bearer t")
    assert run({"name": "f", "type": "rss_freshness", "url": "https://x/private", "headers_env": "FEED_HEADERS"}, session, now).ok


# --- http_freshness --------------------------------------------------------
def test_http_last_modified(session, now):
    session.add("GET", "/file", FakeResponse(200, "x", {"Last-Modified": "Sun, 16 Nov 2026 10:00:00 GMT"}))
    r = run({"name": "h", "type": "http_freshness", "url": "https://x/file"}, session, now)
    assert r.ok and r.age_hours == 2.0


def test_http_missing_last_modified(session, now):
    session.add("GET", "/file", FakeResponse(200, "x"))
    assert run({"name": "h", "type": "http_freshness", "url": "https://x/file"}, session, now).category == "api_error"


@pytest.mark.parametrize("body,field", [
    ({"data": {"updated_at": "2026-11-16T09:00:00Z"}}, "data.updated_at"),
    ({"items": [{"ts": 1794819600000}]}, "items.0.ts"),          # epoch ms = 2026-11-16T09:00:00Z
    ({"items": [{"ts": "1794819600"}]}, "items.-1.ts"),         # epoch seconds as string
])
def test_http_json_field(session, now, body, field):
    session.add("GET", "/api", FakeResponse(200, body))
    r = run({"name": "h", "type": "http_freshness", "url": "https://x/api", "source": "json_field", "field": field}, session, now)
    assert r.ok and r.latest_at == "2026-11-16T09:00:00Z", r.message


def test_http_json_field_missing(session, now):
    session.add("GET", "/api", FakeResponse(200, {"data": {}}))
    r = run({"name": "h", "type": "http_freshness", "url": "https://x/api", "source": "json_field", "field": "data.updated_at"}, session, now)
    assert r.category == "api_error" and "not found" in r.message


def test_http_regex(session, now):
    session.add("GET", "/page", FakeResponse(200, "built 2026-11-15T00:00:00Z ... built 2026-11-16T11:00:00Z"))
    r = run({"name": "h", "type": "http_freshness", "url": "https://x/page", "source": "regex",
             "pattern": r"built (\S+Z)"}, session, now)
    assert r.ok and r.latest_at == "2026-11-16T11:00:00Z"


def test_http_unknown_source(session, now):
    session.add("GET", "/page", FakeResponse(200, "x"))
    assert run({"name": "h", "type": "http_freshness", "url": "https://x/page", "source": "nope"}, session, now).category == "config"


def test_parse_any_date_rejects_garbage():
    from quiet_pipelines.checks.base import CheckError
    with pytest.raises(CheckError):
        parse_any_date("not a date")


# --- repo_file_freshness ---------------------------------------------------
def commit(date, login="dev", name="Dev"):
    return {"sha": "abcdef1234", "author": {"login": login}, "commit": {"author": {"name": name, "date": date}, "committer": {"date": date}}}


def test_repo_file_fresh(session, now, monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    session.add("GET", "api.github.com/repos/o/r/commits", FakeResponse(200, [commit("2026-11-16T08:00:00Z")]),
                when=lambda c: c["params"]["path"] == "out/reg.jsonl")
    r = run({"name": "rf", "type": "repo_file_freshness", "path": "out/reg.jsonl"}, session, now)
    assert r.ok and r.details["sha"] == "abcdef1"
    assert session.calls[0]["headers"]["Authorization"] == "Bearer t"


def test_repo_ignore_authors(session, now, monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    session.add("GET", "/commits", FakeResponse(200, [commit("2026-11-16T08:00:00Z", login="github-actions[bot]"),
                                                       commit("2026-11-10T00:00:00Z")]))
    r = run({"name": "rf", "type": "repo_file_freshness", "path": "p", "ignore_authors": ["github-actions[bot]"]}, session, now)
    assert r.category == "no_output" and r.latest_at == "2026-11-10T00:00:00Z"


def test_repo_no_commits_and_no_repo(session, now, monkeypatch):
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    assert run({"name": "rf", "type": "repo_file_freshness", "path": "p"}, session, now).category == "config"
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    session.add("GET", "/commits", FakeResponse(200, []))
    assert run({"name": "rf", "type": "repo_file_freshness", "path": "p"}, session, now).category == "no_output"


# --- plug-in registry ------------------------------------------------------
def test_custom_check_can_be_registered(session, now):
    from tests.conftest import NOW

    @register
    class Always(BaseCheck):
        type_name = "always_fresh_test"

        def latest_output(self):
            return NOW, {"x": 1}

    try:
        assert run({"name": "c", "type": "always_fresh_test"}, session, now).ok
    finally:
        REGISTRY.pop("always_fresh_test")


def test_unknown_type_raises():
    with pytest.raises(KeyError):
        build({"name": "x", "type": "nope"})


def test_bug_inside_check_does_not_crash(session, now):
    session.add("GET", "/api", FakeResponse(200, {"data": [1, 2]}))
    r = run({"name": "h", "type": "http_freshness", "url": "https://x/api", "source": "json_field", "field": "data.5"}, session, now)
    assert r.status == "abnormal" and r.category == "api_error"
