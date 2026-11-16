from datetime import date
from pathlib import Path

import pytest

from quiet_pipelines.config import ConfigError, load, parse
from quiet_pipelines.ledger import evaluate

TODAY = date(2026, 11, 16)
EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "quiet-pipelines.yml"


def item(**kw):
    return {"id": kw.pop("id", "x"), "kind": "api_key", "name": "SOME_KEY", "last_verified": "2026-11-10", **kw}


def ids(rows):
    return [r["id"] for r in rows]


def test_ledger_buckets():
    rep = evaluate([
        item(id="gone", expires="2026-11-01"),
        item(id="broken", status="broken"),
        item(id="soon", expires="2026-12-01"),
        item(id="later", expires="2027-06-01"),
        item(id="unknown"),
        item(id="unused", status="unused"),
    ], TODAY, warn_within_days=30)
    assert ids(rep.expired_or_broken) == ["gone", "broken"]
    assert ids(rep.expiring_soon) == ["soon"] and rep.expiring_soon[0]["days_left"] == 15
    assert ids(rep.unknown) == ["unknown"]
    assert ids(rep.unused) == ["unused"]
    assert rep.needs_attention


def test_ledger_ok_when_nothing_due():
    rep = evaluate([item(id="later", expires="2027-06-01"), item(id="u")], TODAY)
    assert not rep.needs_attention


def test_ledger_invalid_and_stale():
    rep = evaluate([
        {"id": "a", "kind": "pat"},                              # no name
        item(id="dup"), item(id="dup"),
        item(id="bad", expires="31/12/2026"),
        item(id="weird", status="maybe"),
        item(id="old", last_verified="2026-01-01"),
        {"id": "never", "kind": "model", "name": "m"},           # never verified
    ], TODAY, stale_after_days=30)
    assert sorted(r["problem"].split()[0] for r in rep.invalid) == ["bad", "duplicate", "missing", "unknown"]
    assert set(ids(rep.stale)) == {"old", "never"}


def test_ledger_stale_disabled():
    assert evaluate([{"id": "n", "kind": "k", "name": "n"}], TODAY, stale_after_days=None).stale == []


def test_example_config_is_valid():
    cfg = load(EXAMPLE)
    assert [c["type"] for c in cfg.checks] == ["youtube_latest_upload", "rss_freshness", "http_freshness", "repo_file_freshness"]
    assert cfg.checks[0]["max_age_hours"] == 36 and cfg.checks[1]["max_age_hours"] == 192   # defaults merged, override kept
    assert cfg.diagnose.model == "claude-opus-5-5" and cfg.issues.label == "quiet-pipelines"
    assert evaluate(cfg.ledger_items, TODAY).invalid == []


@pytest.mark.parametrize("data,msg", [
    ({"checks": [{"name": "a", "type": "nope"}]}, "unknown type"),
    ({"checks": [{"type": "rss_freshness"}]}, "name is required"),
    ({"checks": [{"name": "a", "type": "rss_freshness"}, {"name": "a", "type": "rss_freshness"}]}, "duplicate"),
    ({"diagnose": {"modle": "x"}}, "unknown key"),
    ({"version": 2}, "unsupported version"),
    ({"checks": ["x"]}, "must be a mapping"),
])
def test_config_errors(data, msg):
    with pytest.raises(ConfigError, match=msg):
        parse(data)


def test_disabled_checks_are_skipped():
    cfg = parse({"checks": [{"name": "a", "type": "rss_freshness", "url": "u", "enabled": False}]})
    assert cfg.checks == []


def test_missing_file():
    with pytest.raises(ConfigError, match="not found"):
        load("does-not-exist.yml")
