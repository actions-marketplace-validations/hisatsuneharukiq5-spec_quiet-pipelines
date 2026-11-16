from datetime import date, timedelta

import pytest

from quiet_pipelines.cli import main
from quiet_pipelines.config import parse
from quiet_pipelines.diagnose import Diagnosis
from quiet_pipelines.github import GitHub
from quiet_pipelines.issues import sync_all
from quiet_pipelines.ledger import evaluate
from tests.conftest import NOW, FakeResponse


class MemGH:
    """In-memory GitHub issues."""

    def __init__(self):
        self.issues, self.comments_by = {}, {}
        self.n = 0

    def open_issues(self, label):
        return [i for i in self.issues.values() if i["state"] == "open" and label in i["labels"]]

    def comments(self, number):
        return self.comments_by.get(number, [])

    def create_issue(self, title, body, labels, assignees=None):
        self.n += 1
        self.issues[self.n] = {"number": self.n, "title": title, "body": body, "labels": labels, "state": "open",
                               "created_at": self.clock.isoformat()}
        return {"number": self.n}

    def comment(self, number, body):
        self.comments_by.setdefault(number, []).append({"body": body, "created_at": self.clock.isoformat()})

    def close(self, number):
        self.issues[number]["state"] = "closed"


CFG = parse({"checks": [{"name": "yt", "type": "rss_freshness", "url": "u"}], "issues": {"repeat_comment_hours": 24}})


def res(status="abnormal", category="no_output", msg="newest output is 50h old"):
    return {"name": "yt", "type": "rss_freshness", "status": status, "category": None if status == "ok" else category,
            "message": msg, "latest_at": "2026-11-14T10:00:00Z", "age_hours": 50, "max_age_hours": 36}


def step(gh, result, at, ledger=None, diag_calls=None):
    gh.clock = at

    def diagnose_for(r):
        if diag_calls is not None:
            diag_calls.append(r["name"])
        return Diagnosis(category="quota", confidence="high", summary="Quota exhausted.", human_steps=["Wait for reset."])
    return sync_all(gh, [result], ledger, CFG, now=at, diagnose_for=diagnose_for)


def test_issue_lifecycle():
    gh, calls = MemGH(), []
    a = step(gh, res(), NOW, diag_calls=calls)
    assert a[0]["action"] == "opened" and calls == ["yt"]
    issue = gh.issues[1]
    assert "quiet-pipelines:key=check:yt" in issue["body"] and "Wait for reset." in issue["body"] and "Quota" in issue["body"]

    # same state 1h later -> nothing, no new diagnosis
    assert step(gh, res(msg="51h"), NOW + timedelta(hours=1), diag_calls=calls)[0]["action"] == "skipped"
    assert calls == ["yt"] and len(gh.issues) == 1

    # same state 25h later -> reminder comment, still no diagnosis
    a = step(gh, res(msg="75h"), NOW + timedelta(hours=25), diag_calls=calls)
    assert a[0]["action"] == "commented" and not a[0]["changed"] and calls == ["yt"]

    # category changes -> comment + new diagnosis, still one issue
    a = step(gh, res(category="auth"), NOW + timedelta(hours=26), diag_calls=calls)
    assert a[0]["action"] == "commented" and a[0]["changed"] and calls == ["yt", "yt"] and len(gh.issues) == 1

    # recovered -> close
    a = step(gh, res(status="ok", msg="newest output is 1h old"), NOW + timedelta(hours=27))
    assert a[0]["action"] == "closed" and gh.issues[1]["state"] == "closed"

    # broken again -> a NEW issue
    assert step(gh, res(), NOW + timedelta(hours=40))[0]["action"] == "opened" and len(gh.issues) == 2


def test_ok_without_issue_does_nothing():
    gh = MemGH()
    assert step(gh, res(status="ok"), NOW)[0]["action"] == "none" and gh.issues == {}


def test_ledger_issue():
    gh = MemGH()
    rep = evaluate([{"id": "pat", "kind": "pat", "name": "GH_PAT", "expires": (NOW.date() + timedelta(days=5)).isoformat(),
                     "last_verified": "2026-11-16", "check": "settings page"}], NOW.date())
    acts = step(gh, res(status="ok"), NOW, ledger=rep)
    led = next(a for a in acts if a["key"] == "ledger")
    assert led["action"] == "opened" and "GH_PAT" in gh.issues[1]["body"] and "5d" in gh.issues[1]["body"]
    fixed = evaluate([{"id": "pat", "kind": "pat", "name": "GH_PAT", "expires": "2027-12-01", "last_verified": "2026-11-16"}], NOW.date())
    acts = step(gh, res(status="ok"), NOW + timedelta(hours=1), ledger=fixed)
    assert next(a for a in acts if a["key"] == "ledger")["action"] == "closed"


def test_japanese_rendering():
    cfg = parse({"diagnose": {"language": "ja"}})
    gh = MemGH()
    gh.clock = NOW
    sync_all(gh, [res()], None, cfg, now=NOW, diagnose_for=lambda r: Diagnosis(category="unknown", summary="不明です", human_steps=["ログを確認"]))
    body = gh.issues[1]["body"]
    assert "考えられる原因" in body and "不明" in body and "1. ログを確認" in body


def test_github_client_dry_run_records_writes(session, monkeypatch):
    session.add("GET", "/repos/o/r/issues", FakeResponse(200, [{"number": 3, "body": "x"}, {"number": 4, "pull_request": {}, "body": "y"}]))
    gh = GitHub(repo="o/r", token="t", session=session, dry_run=True)
    assert [i["number"] for i in gh.open_issues("quiet-pipelines")] == [3]          # PRs filtered out
    gh.create_issue("t", "b", ["quiet-pipelines"])
    gh.comment(3, "c")
    gh.close(3)
    assert [w["method"] for w in gh.writes] == ["POST", "POST", "PATCH"]
    assert all(c["method"] == "GET" for c in session.calls)                         # nothing was written


def test_github_requires_repo(monkeypatch):
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    with pytest.raises(Exception, match="GITHUB_REPOSITORY"):
        GitHub(repo=None, token="t")


# --- CLI -----------------------------------------------------------------
def write(tmp_path, text):
    p = tmp_path / "quiet-pipelines.yml"
    p.write_text(text, encoding="utf-8")
    return str(p)


LEDGER_ONLY = """
version: 1
ledger:
  items:
    - {id: k, kind: api_key, name: K, expires: %s, last_verified: 2026-11-16}
issues: {enabled: false}
"""


def test_cli_validate_and_ledger(tmp_path, capsys):
    soon = (date.today() + timedelta(days=3)).isoformat()
    later = (date.today() + timedelta(days=300)).isoformat()
    assert main(["validate", "--config", write(tmp_path, LEDGER_ONLY % later)]) == 0
    assert main(["ledger", "--config", write(tmp_path, LEDGER_ONLY % later)]) == 0
    assert main(["ledger", "--config", write(tmp_path, LEDGER_ONLY % soon)]) == 1
    assert "~ k" in capsys.readouterr().out


def test_cli_run_report_and_exit_codes(tmp_path):
    soon = (date.today() + timedelta(days=3)).isoformat()
    cfg = write(tmp_path, LEDGER_ONLY % soon)
    out = tmp_path / "r.json"
    assert main(["run", "--config", cfg, "--output", str(out)]) == 0                 # abnormal but not failing by default
    assert '"abnormal": true' in out.read_text(encoding="utf-8")
    assert main(["run", "--config", cfg, "--fail-on-abnormal"]) == 1


def test_cli_config_error(tmp_path, capsys):
    assert main(["validate", "--config", write(tmp_path, "checks: [{name: a, type: nope}]")]) == 2
    assert "unknown type" in capsys.readouterr().err
