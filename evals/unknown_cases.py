"""Live eval: does the diagnosis say "unknown" when the log has no evidence?

Not part of the unit tests — it calls the Claude API and costs money (~$0.01-0.02 per case).

  ANTHROPIC_API_KEY=... python evals/unknown_cases.py [--models claude-sonnet-5-5,claude-opus-5-5] [--budget 0.30]

Cases (synthetic GitHub Actions logs, no real data):
  truncated   — log stops mid-line during a normal step; no error lines at all
  no_error    — complete, ordinary-looking log; only the job metadata says a step failed
  control     — clear invalid_grant traceback (should NOT be unknown)
Expected: truncated/no_error -> unknown (low confidence); control -> expired_or_auth.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quiet_pipelines.config import DiagnoseConfig  # noqa: E402
from quiet_pipelines.diagnose import diagnose  # noqa: E402

PRICES = {"claude-sonnet-5-5": (2.0, 10.0), "claude-opus-5-5": (4.0, 20.0), "claude-haiku-5-5": (0.10, 0.50)}

SETUP = """2026-11-16T03:10:01.0000000Z ##[group]Run actions/checkout@v4
2026-11-16T03:10:02.0000000Z Syncing repository: example/pipeline
2026-11-16T03:10:04.0000000Z ##[endgroup]
2026-11-16T03:10:05.0000000Z ##[group]Run pip install -r requirements.txt
2026-11-16T03:10:40.0000000Z Successfully installed anthropic-2.8.0 edge-tts-7.3.1 requests-2.34.2
2026-11-16T03:10:41.0000000Z ##[endgroup]
2026-11-16T03:10:42.0000000Z ##[group]Run python main.py facts-run-single
2026-11-16T03:10:45.0000000Z INFO facts_scheduler: Facts short generation started (topic=random)
2026-11-16T03:10:52.0000000Z INFO facts_scheduler: Facts script accepted (words=164)
2026-11-16T03:11:05.0000000Z INFO facts_scheduler: Narration duration 57.8s
2026-11-16T03:11:20.0000000Z INFO facts_scheduler: Found 40 clip URLs
2026-11-16T03:11:31.0000000Z INFO facts_scheduler: Downloading clip 1/20
2026-11-16T03:11:39.0000000Z INFO facts_scheduler: Downloading clip 2/20
"""
CASES = {
    "truncated": SETUP + "2026-11-16T03:11:47.0000000Z INFO facts_scheduler: Downloading clip 3/2",
    "no_error": SETUP + """2026-11-16T03:13:02.0000000Z INFO facts_scheduler: Downloading clip 20/20
2026-11-16T03:15:40.0000000Z INFO facts_scheduler: Composing vertical track
2026-11-16T03:17:12.0000000Z INFO facts_scheduler: Mixing audio
2026-11-16T03:17:30.0000000Z ##[endgroup]
2026-11-16T03:17:31.0000000Z Post job cleanup.
2026-11-16T03:17:32.0000000Z Cleaning up orphan processes
""",
    "control": SETUP + """2026-11-16T03:10:46.0000000Z Traceback (most recent call last):
2026-11-16T03:10:46.0000000Z   File "facts_scheduler.py", line 403, in _authenticate_youtube
2026-11-16T03:10:46.0000000Z google.auth.exceptions.RefreshError: ('invalid_grant: Token has been expired or revoked.', {'error': 'invalid_grant'})
2026-11-16T03:10:46.0000000Z ##[error]Process completed with exit code 1.
""",
}
EXPECT = {"truncated": "unknown", "no_error": "unknown", "control": "expired_or_auth"}
CHECK = {"name": "facts-channel", "type": "youtube_latest_upload", "status": "abnormal", "category": "no_output",
         "message": "newest public video is 50.0h old (threshold 36h)", "latest_at": None, "age_hours": 50.0, "max_age_hours": 36}


class SyntheticGH:
    def __init__(self, log: str):
        self.log = log

    def failed_runs(self, workflows, lookback):
        return [{"id": 1, "name": "Daily Auto-Post", "html_url": None, "created_at": "2026-11-16T03:10:00Z", "event": "schedule"}]

    def failed_attempts(self, workflows, lookback):
        return []

    def failed_jobs(self, run_id, attempt=None):
        return [{"id": 11, "name": "post-facts-short", "steps": [{"name": "Post one Facts short", "conclusion": "failure"}]}]

    def job_log(self, job_id):
        return self.log


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="claude-sonnet-5-5")
    ap.add_argument("--cases", default="truncated,no_error,control")
    ap.add_argument("--budget", type=float, default=0.30)
    args = ap.parse_args()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 2

    spent, rows = 0.0, []
    for model in args.models.split(","):
        for case in args.cases.split(","):
            if spent >= args.budget * 0.8:
                print(f"budget guard: spent ${spent:.4f}, stopping before {model}/{case}")
                break
            d = diagnose(CHECK, DiagnoseConfig(model=model, language="en"), gh=SyntheticGH(CASES[case]))
            pin, pout = PRICES.get(model, (4.0, 20.0))
            usd = d.usage.get("input_tokens", 0) * pin / 1e6 + d.usage.get("output_tokens", 0) * pout / 1e6
            spent += usd
            ok = d.category == EXPECT[case]
            rows.append({"model": model, "case": case, "expected": EXPECT[case], "got": d.category, "confidence": d.confidence,
                         "pass": ok, "usd": round(usd, 5), "usage": d.usage, "summary": d.summary[:300],
                         "steps": d.human_steps, "evidence": d.evidence, "error": d.error})
    print(json.dumps({"total_usd": round(spent, 5), "results": rows}, ensure_ascii=False, indent=2))
    return 0 if all(r["pass"] for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
