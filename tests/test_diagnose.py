import json
from types import SimpleNamespace

import pytest

from quiet_pipelines.config import DiagnoseConfig
from quiet_pipelines.diagnose import collect_logs, diagnose, excerpt

SECRET = "ghp_" + "Zx9Yw8Vu7Ts6Rq5Po4Nm3Lk2Ji1Hg0FeDcBa"
CHECK = {"name": "yt", "type": "youtube_latest_upload", "status": "abnormal", "category": "auth",
         "message": "token refresh failed: HTTP 400 invalid_grant", "latest_at": None, "age_hours": None, "max_age_hours": 36}


class FakeClient:
    """Stands in for anthropic.Anthropic(); records the request."""

    def __init__(self, payload=None, stop_reason="end_turn", raw_text=None, raises=None):
        self.kwargs = None
        self._payload, self._stop, self._raw, self._raises = payload, stop_reason, raw_text, raises
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.kwargs = kwargs
        if self._raises:
            raise self._raises
        text = self._raw if self._raw is not None else json.dumps(self._payload)
        return SimpleNamespace(stop_reason=self._stop, model=kwargs["model"],
                               content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
                               usage=SimpleNamespace(input_tokens=1200, output_tokens=300))


class FakeGH:
    def __init__(self, log):
        self.log = log

    def failed_runs(self, workflows, lookback_hours):
        self.args = (workflows, lookback_hours)
        return [{"id": 7, "name": "daily", "html_url": "https://github.com/o/r/actions/runs/7", "created_at": "2026-11-16T01:00:00Z", "event": "schedule"}]

    def failed_jobs(self, run_id):
        return [{"id": 70, "name": "post", "steps": [{"name": "Upload", "conclusion": "failure"}, {"name": "Setup", "conclusion": "success"}]}]

    def job_log(self, job_id):
        return self.log


GOOD = {"category": "expired_or_auth", "confidence": "high", "summary": "The OAuth refresh token was revoked.",
        "evidence": ["invalid_grant: Bad Request"], "human_steps": ["Re-authorize the channel.", "Update the secret."]}


def test_happy_path_and_request_shape(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", SECRET)
    log = f"step 1\nUsing token {SECRET}\ngoogle.auth.exceptions.RefreshError: invalid_grant: Bad Request\n"
    client, gh = FakeClient(GOOD), FakeGH(log)
    d = diagnose(CHECK, DiagnoseConfig(language="ja"), gh=gh, client=client, workflows=["daily.yml"])

    assert d.category == "expired_or_auth" and d.confidence == "high" and d.label("ja") == "期限切れ・認証"
    assert d.human_steps == GOOD["human_steps"] and d.run_url.endswith("/runs/7")
    assert d.usage == {"input_tokens": 1200, "output_tokens": 300}
    assert gh.args == (["daily.yml"], 48)

    sent = client.kwargs
    prompt = sent["messages"][0]["content"]
    assert SECRET not in prompt and "[REDACTED]" in prompt and "invalid_grant" in prompt
    assert "failed steps: Upload" in prompt and "Japanese" in prompt
    assert sent["model"] == "claude-sonnet-5-5"
    assert sent["output_config"]["format"]["type"] == "json_schema" and sent["output_config"]["effort"] == "medium"
    assert sent["fallbacks"] == "default" and sent["betas"] == ["server-side-fallback-2026-07-01"]
    assert "temperature" not in sent and "thinking" not in sent
    assert d.redactions.get("known_value", 0) + d.redactions.get("github_token", 0) >= 1


def test_low_confidence_is_forced_to_unknown():
    payload = {**GOOD, "category": "quota", "confidence": "low", "summary": "maybe quota"}
    d = diagnose(CHECK, DiagnoseConfig(), gh=FakeGH("x"), client=FakeClient(payload))
    assert d.category == "unknown" and "leaned toward 'quota'" in d.summary


def test_invalid_category_becomes_unknown():
    d = diagnose(CHECK, DiagnoseConfig(), gh=FakeGH("x"), client=FakeClient({**GOOD, "category": "cosmic_rays"}))
    assert d.category == "unknown"


@pytest.mark.parametrize("client,err", [
    (FakeClient(GOOD, stop_reason="refusal"), "refusal"),
    (FakeClient(GOOD, stop_reason="max_tokens"), "truncated"),
    (FakeClient(raw_text="not json"), "non-JSON"),
    (FakeClient(raises=RuntimeError("503 overloaded")), "Claude API call failed"),
])
def test_failures_degrade_to_unknown(client, err):
    d = diagnose(CHECK, DiagnoseConfig(), gh=FakeGH("x"), client=client)
    assert d.category == "unknown" and err in d.error


def test_no_api_key_skips_without_calling(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    d = diagnose(CHECK, DiagnoseConfig(), gh=None, client=None)
    assert d.category == "unknown" and "not set" in d.error


def test_model_output_is_redacted_again():
    payload = {**GOOD, "summary": f"token {SECRET} expired", "human_steps": [f"rotate {SECRET}"]}
    d = diagnose(CHECK, DiagnoseConfig(), gh=FakeGH("x"), client=FakeClient(payload))
    assert SECRET not in d.summary and SECRET not in d.human_steps[0]


def test_non_fallback_model_has_no_fallbacks():
    client = FakeClient(GOOD)
    diagnose(CHECK, DiagnoseConfig(model="claude-haiku-5-5"), gh=FakeGH("x"), client=client)
    assert "fallbacks" not in client.kwargs and "betas" not in client.kwargs


def test_api_error_text_is_redacted(monkeypatch):
    # diag.error goes into a (possibly public) Issue: pattern-shaped secrets are masked...
    d = diagnose(CHECK, DiagnoseConfig(), gh=FakeGH("x"),
                 client=FakeClient(raises=RuntimeError(f"401 bad header Authorization: Bearer {SECRET}")))
    assert "Claude API call failed" in d.error and SECRET not in d.error and "[REDACTED]" in d.error


def test_api_error_redacts_known_env_value_before_truncation(monkeypatch):
    # ...and so is the exact value of a secret-named env var, even with no known shape
    # and even when it sits right at the truncation point (160 chars).
    fake_key = "plain-looking-value-0042"
    monkeypatch.setenv("ANTHROPIC_API_KEY", fake_key)
    msg = "x" * 150 + fake_key
    d = diagnose(CHECK, DiagnoseConfig(), gh=FakeGH("x"), client=FakeClient(raises=RuntimeError(msg)))
    assert fake_key not in d.error and fake_key[:10] not in d.error and "[REDACTED]" in d.error


def test_log_read_error_text_is_redacted():
    class LeakyGH(FakeGH):
        def failed_runs(self, *a):
            raise RuntimeError(f"GET https://user:{SECRET}@api.github.com/x failed")
    d = diagnose(CHECK, DiagnoseConfig(), gh=LeakyGH(""), client=FakeClient(GOOD))
    assert "could not read CI logs" in d.error and SECRET not in d.error


def test_log_read_failure_still_diagnoses():
    class BrokenGH(FakeGH):
        def failed_runs(self, *a):
            raise RuntimeError("HTTP 403")
    client = FakeClient(GOOD)
    d = diagnose(CHECK, DiagnoseConfig(), gh=BrokenGH(""), client=client)
    assert d.category == "expired_or_auth" and "could not read CI logs" in d.error
    assert "No failed run was found" in client.kwargs["messages"][0]["content"]


def test_excerpt_keeps_error_lines_and_tail():
    log = "\n".join(["noise"] * 5000 + ["ERROR: quotaExceeded at upload"] + ["noise"] * 5000 + ["final line"])
    text, truncated = excerpt(log, 4000)
    assert truncated and "quotaExceeded" in text and text.rstrip().endswith("final line") and len(text) < 4200
    short, t2 = excerpt("small log", 4000)
    assert short == "small log" and not t2


def test_collect_logs_no_failed_runs():
    class NoRuns(FakeGH):
        def failed_runs(self, *a):
            return []
    assert collect_logs(NoRuns(""), None, 48, 1000) == (None, "")
