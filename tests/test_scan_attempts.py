"""Re-run attempts, hidden-failure log scan, swallowed-warning issues, AI-suggestion wording."""
from datetime import timedelta

import pytest

from quiet_pipelines.config import ConfigError, DEFAULT_MODEL, DiagnoseConfig, parse
from quiet_pipelines.diagnose import Diagnosis, collect_logs
from quiet_pipelines.github import GitHub
from quiet_pipelines.issues import render_diagnosis, sync_all
from quiet_pipelines.scan import ScanConfig, compile_patterns, scan_log, scan_successful_runs
from tests.conftest import NOW, FakeResponse
from tests.test_issues_cli import MemGH, res

PAT = "ghp_" + "Q1w2E3r4T5y6U7i8O9p0A1s2D3f4G5h6J7k8"
LOG = f"""2026-11-15T16:52:20.1000000Z ##[group]Run actions/checkout@v4
2026-11-15T16:52:21.1000000Z Syncing repository
2026-11-15T16:52:22.1000000Z ##[endgroup]
2026-11-15T16:53:00.1000000Z ##[group]Run git push || true
2026-11-15T16:53:01.4030000Z remote: Permission to o/r.git denied to someone.
2026-11-15T16:53:01.5000000Z fatal: unable to access 'https://{PAT}@github.com/o/r/': The requested URL returned error: 403
2026-11-15T16:53:02.1000000Z ##[group]Run gh secret set X
2026-11-15T16:53:03.1000000Z failed to set secret "X": HTTP 403: Resource not accessible by personal access token
2026-11-15T16:53:04.1000000Z max_tokens=16000 input_tokens: 4030
"""


def pats(**kw):
    return compile_patterns(ScanConfig(**kw))


# --- scan_log ---------------------------------------------------------------
def test_scan_log_finds_swallowed_errors_with_steps_and_redaction():
    hits = scan_log(LOG, *pats(), limit=10)
    assert [h[0] for h in hits] == ["git push || true", "git push || true", "gh secret set X"]
    assert "denied" in hits[0][2] and "403" in hits[1][2]
    assert PAT not in hits[1][2] and "[REDACTED]" in hits[1][2]
    # timestamps ("...4030000Z") and token counts are not mistaken for HTTP 403
    assert not any("max_tokens" in h[2] for h in hits)


def test_scan_log_ignore_limit_and_custom_patterns():
    assert len(scan_log(LOG, *pats(ignore=[r"Permission to"]), limit=10)) == 2
    assert len(scan_log(LOG, *pats(), limit=1)) == 1
    only = scan_log(LOG, *pats(patterns=[r"Syncing repository"]), limit=10)
    assert [(h[0], h[2]) for h in only] == [("actions/checkout@v4", "Syncing repository")]
    extra = scan_log("all good\nWARN disk almost full", *pats(extra_patterns=[r"disk almost full"]), limit=10)
    assert len(extra) == 1


def test_clean_log_has_no_warnings():
    clean = "2026-11-16T00:00:00.0000000Z ##[group]Run python main.py\n2026-11-16T00:00:01.0000000Z Facts short upload complete\n"
    assert scan_log(clean, *pats(), limit=10) == []


def test_invalid_regex_is_config_error():
    with pytest.raises(ConfigError, match="invalid regex"):
        parse({"log_scan": {"extra_patterns": ["(unclosed"]}})


def test_log_scan_config_and_workflow_union():
    cfg = parse({"checks": [{"name": "a", "type": "rss_freshness", "url": "u", "workflows": ["x.yml", "y.yml"]},
                            {"name": "b", "type": "rss_freshness", "url": "u", "workflows": ["y.yml", "z.yml"]}],
                 "log_scan": {"lookback_hours": 12, "ignore": ["harmless"]}})
    assert cfg.check_workflows() == ["x.yml", "y.yml", "z.yml"]
    assert cfg.log_scan.lookback_hours == 12 and cfg.log_scan.ignore == ["harmless"]
    assert cfg.log_scan.enabled and "\\binvalid_grant\\b" in cfg.log_scan.patterns


class ScanGH:
    def __init__(self, runs, jobs, logs):
        self._runs, self._jobs, self._logs, self.asked = runs, jobs, logs, None

    def runs(self, workflows, lookback, status=None):
        self.asked = (workflows, lookback, status)
        return self._runs

    def jobs(self, run_id, attempt=None):
        return self._jobs.get(run_id, [])

    def job_log(self, job_id):
        if job_id not in self._logs:
            raise RuntimeError("HTTP 410 log expired")
        return self._logs[job_id]


def test_scan_successful_runs(monkeypatch):
    monkeypatch.setenv("GITHUB_WORKFLOW", "quiet-pipelines")
    gh = ScanGH(
        runs=[{"id": 1, "name": "Daily Analysis", "html_url": "u1", "created_at": "2026-11-15T16:52:22Z"},
              {"id": 2, "name": "quiet-pipelines", "html_url": "u2"},                 # the watchdog itself: skipped
              {"id": 3, "name": "Growth", "html_url": "u3"}],
        jobs={1: [{"id": 10, "name": "analyze", "conclusion": "success"}, {"id": 11, "name": "skipped", "conclusion": "skipped"}],
              2: [{"id": 20, "name": "watch", "conclusion": "success"}],
              3: [{"id": 30, "name": "grow", "conclusion": "success"}]},
        logs={10: LOG, 20: LOG})                                                       # job 30's log expired
    warns = scan_successful_runs(gh, ScanConfig(lookback_hours=24), ["facts_analyze.yml"])
    assert gh.asked == (["facts_analyze.yml"], 24, "success")
    assert {w.run_id for w in warns} == {1} and len(warns) == 3
    assert warns[0].job == "analyze" and warns[0].html_url == "u1"


# --- attempts ---------------------------------------------------------------
def test_github_failed_attempts(session):
    session.add("GET", "/actions/runs", FakeResponse(200, {"workflow_runs": [
        {"id": 5, "run_attempt": 2, "conclusion": "success", "html_url": "https://g/runs/5", "created_at": "2026-11-15T16:52:22Z"},
        {"id": 6, "run_attempt": 1, "conclusion": "success", "html_url": "https://g/runs/6", "created_at": "2026-11-15T17:00:00Z"}]}),
        when=lambda c: c["url"].endswith("/actions/runs"))
    session.add("GET", "/actions/runs/5/attempts/1", FakeResponse(200, {"id": 5, "run_attempt": 1, "conclusion": "failure",
                                                                        "name": "Daily Analysis", "run_started_at": "2026-11-15T16:52:22Z"}),
                when=lambda c: c["url"].endswith("/attempts/1"))
    session.add("GET", "/actions/runs/5/attempts/1/jobs", FakeResponse(200, {"jobs": [{"id": 50, "conclusion": "failure"}, {"id": 51, "conclusion": "success"}]}))
    gh = GitHub(repo="o/r", token="t", session=session)
    atts = gh.failed_attempts(None, 48, now=NOW)
    assert [(a["id"], a["run_attempt"], a["html_url"]) for a in atts] == [(5, 1, "https://g/runs/5/attempts/1")]
    assert [j["id"] for j in gh.failed_jobs(5, 1)] == [50]
    assert not any("/runs/6/attempts" in c["url"] for c in session.calls)        # single-attempt runs not expanded


class AttemptGH:
    def __init__(self, failed=(), attempts=()):
        self._f, self._a, self.jobs_asked = list(failed), list(attempts), []

    def failed_runs(self, workflows, lookback):
        return self._f

    def failed_attempts(self, workflows, lookback):
        return self._a

    def failed_jobs(self, run_id, attempt=None):
        self.jobs_asked.append((run_id, attempt))
        return [{"id": run_id * 10, "name": "job", "steps": [{"name": "Rotate token", "conclusion": "failure"}]}]

    def job_log(self, job_id):
        return f"log of job {job_id}"


def test_collect_logs_prefers_newest_including_earlier_attempts():
    gh = AttemptGH(failed=[{"id": 1, "name": "old", "created_at": "2026-11-14T00:00:00Z"}],
                   attempts=[{"id": 5, "name": "Daily Analysis", "created_at": "2026-11-15T16:52:22Z", "run_attempt": 1,
                              "html_url": "https://g/runs/5/attempts/1"}])
    meta, text = collect_logs(gh, None, 48, 10_000)
    assert meta["id"] == 5 and meta["attempt"] == 1 and meta["earlier_attempt_of_rerun"] is True
    assert gh.jobs_asked == [(5, 1)] and "log of job 50" in text and "Rotate token" in text


def test_collect_logs_can_ignore_attempts():
    gh = AttemptGH(attempts=[{"id": 5, "created_at": "2026-11-15T00:00:00Z", "run_attempt": 1}])
    assert collect_logs(gh, None, 48, 1000, include_attempts=False) == (None, "")


# --- issues -------------------------------------------------------------------
def test_swallowed_warning_issue_lifecycle():
    cfg = parse({"issues": {"repeat_comment_hours": 24}})
    gh = MemGH()
    w = {"run_name": "Daily Analysis", "html_url": "u", "created_at": "t", "job": "analyze", "step": "git push || true",
         "pattern": r"\bdenied\b", "line": "remote: Permission to o/r.git denied"}

    def go(warnings, at):
        gh.clock = at
        return next(a for a in sync_all(gh, [], None, cfg, now=at, warnings=warnings) if a["key"] == "swallowed")

    assert go([], NOW)["action"] == "none"
    assert go([w], NOW)["action"] == "opened" and "git push || true" in gh.issues[1]["body"]
    assert go([dict(w, created_at="t2")], NOW + timedelta(hours=1))["action"] == "skipped"     # same signature
    assert go([w, dict(w, step="gh secret set", pattern="Resource not accessible")], NOW + timedelta(hours=2))["action"] == "commented"
    assert go([], NOW + timedelta(hours=3))["action"] == "closed"


def test_no_warnings_key_when_scan_disabled():
    gh = MemGH()
    gh.clock = NOW
    acts = sync_all(gh, [res(status="ok")], None, parse({}), now=NOW, warnings=None)
    assert [a["key"] for a in acts] == ["check:yt"]


@pytest.mark.parametrize("lang,note,steps", [
    ("en", "generated by an AI model", "Suggested steps — AI-generated, verify each one before acting"),
    ("ja", "AI が作成したもの", "提案された手順（AI が自動生成。実行する前に1つずつ内容を確認してください）"),
])
def test_diagnosis_is_labelled_as_ai_suggestion(lang, note, steps):
    body = render_diagnosis(Diagnosis(category="quota", confidence="high", summary="s", human_steps=["a", "b"]), lang)
    assert note in body and steps in body and "1. a" in body and "2. b" in body


def test_default_model_is_sonnet_but_configurable():
    assert DEFAULT_MODEL == "claude-sonnet-5-5" and DiagnoseConfig().model == "claude-sonnet-5-5"
    assert parse({"diagnose": {"model": "claude-opus-5-5"}}).diagnose.model == "claude-opus-5-5"
