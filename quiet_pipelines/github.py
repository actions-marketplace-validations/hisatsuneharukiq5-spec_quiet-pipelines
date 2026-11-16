"""Minimal GitHub REST client: failed runs + job logs (read), issues (write).

Designed for the built-in Actions GITHUB_TOKEN with:
  permissions: { contents: read, actions: read, issues: write }
so the watchdog never depends on a personal access token it might be watching.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

import requests


class GitHubError(RuntimeError):
    pass


class GitHub:
    def __init__(self, repo: str | None = None, token: str | None = None, api: str | None = None,
                 session: requests.Session | None = None, dry_run: bool = False, timeout: float = 30.0):
        self.repo = repo or os.environ.get("GITHUB_REPOSITORY", "")
        self.token = token if token is not None else os.environ.get("GITHUB_TOKEN", "")
        self.api = (api or os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
        self.session = session or requests.Session()
        self.dry_run = dry_run
        self.timeout = timeout
        self.writes: list[dict[str, Any]] = []  # what was (or would be) written — for reports/tests
        if not self.repo or "/" not in self.repo:
            raise GitHubError("GITHUB_REPOSITORY (owner/name) is not set")

    # -- plumbing ---------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _req(self, method: str, path: str, **kw: Any) -> requests.Response:
        url = path if path.startswith("http") else f"{self.api}/repos/{self.repo}{path}"
        resp = self.session.request(method, url, headers=self._headers(), timeout=self.timeout, **kw)
        if resp.status_code >= 400:
            raise GitHubError(f"{method} {path}: HTTP {resp.status_code}")
        return resp

    def _write(self, method: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.writes.append({"method": method, "path": path, "payload": payload})
        if self.dry_run:
            return {"dry_run": True, "number": None, "html_url": None}
        return self._req(method, path, json=payload).json()

    # -- runs / logs (read) -------------------------------------------------
    def failed_runs(self, workflows: list[str] | None = None, lookback_hours: int = 48,
                    now: datetime | None = None) -> list[dict[str, Any]]:
        """Most recent failed runs (newest first), optionally limited to workflow file names."""
        now = now or datetime.now(timezone.utc)
        since = (now - timedelta(hours=lookback_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        params = {"status": "failure", "per_page": 20, "created": f">={since}"}
        runs: list[dict[str, Any]] = []
        targets = workflows or [None]
        for wf in targets:
            path = f"/actions/workflows/{wf}/runs" if wf else "/actions/runs"
            runs.extend(self._req("GET", path, params=params).json().get("workflow_runs", []))
        runs.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        return runs

    def failed_jobs(self, run_id: int) -> list[dict[str, Any]]:
        jobs = self._req("GET", f"/actions/runs/{run_id}/jobs", params={"per_page": 50}).json().get("jobs", [])
        return [j for j in jobs if j.get("conclusion") == "failure"]

    def job_log(self, job_id: int) -> str:
        # 302 to a signed blob URL; requests drops Authorization on cross-host redirects.
        return self._req("GET", f"/actions/jobs/{job_id}/logs", allow_redirects=True).text

    # -- issues ---------------------------------------------------------------
    def open_issues(self, label: str) -> list[dict[str, Any]]:
        items = self._req("GET", "/issues", params={"state": "open", "labels": label, "per_page": 100}).json()
        return [i for i in items if "pull_request" not in i]

    def comments(self, number: int) -> list[dict[str, Any]]:
        """Up to the last 100 comments, oldest first."""
        return self._req("GET", f"/issues/{number}/comments", params={"per_page": 100}).json()

    def create_issue(self, title: str, body: str, labels: list[str], assignees: list[str] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"title": title, "body": body, "labels": labels}
        if assignees:
            payload["assignees"] = assignees
        return self._write("POST", "/issues", payload)

    def comment(self, number: int, body: str) -> dict[str, Any]:
        return self._write("POST", f"/issues/{number}/comments", {"body": body})

    def close(self, number: int) -> dict[str, Any]:
        return self._write("PATCH", f"/issues/{number}", {"state": "closed", "state_reason": "completed"})
