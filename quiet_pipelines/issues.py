"""Turn check / ledger results into GitHub Issues — one open issue per problem.

- abnormal, no open issue      -> open one (with diagnosis)
- abnormal, issue already open -> comment only if the state changed, or
                                  `repeat_comment_hours` passed since our last comment
- ok, issue open               -> comment "recovered" and close it

Issues are matched by a hidden marker in the body, so renaming titles is safe.
State (category + short fingerprint) is stored in hidden markers in our comments.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Callable

from .diagnose import Diagnosis

MARK = "<!-- quiet-pipelines:key={key} -->"
STATE = "<!-- quiet-pipelines:state={state} -->"
_STATE_RE = re.compile(r"<!-- quiet-pipelines:state=([^ ]+) -->")

TEXT = {
    "en": {"opened": "Detected", "status": "Status", "why": "Why", "newest": "Newest output", "threshold": "Threshold",
           "diag": "Diagnosis", "cause": "Likely cause", "confidence": "Confidence", "steps": "What to do",
           "evidence": "Evidence from the log", "run": "Failed run", "none": "(no diagnosis)",
           "still": "Still abnormal", "changed": "State changed", "recovered": "Recovered",
           "footer": "Managed by quiet-pipelines. This issue closes automatically when the check recovers.",
           "ledger_title": "Expiry ledger: items need attention", "expired": "Expired / broken", "soon": "Expiring soon"},
    "ja": {"opened": "検知", "status": "状態", "why": "内容", "newest": "最新の成果", "threshold": "閾値",
           "diag": "診断", "cause": "考えられる原因", "confidence": "確信度", "steps": "やること",
           "evidence": "ログ上の根拠", "run": "失敗した実行", "none": "（診断なし）",
           "still": "異常が継続中", "changed": "状態が変わりました", "recovered": "回復しました",
           "footer": "quiet-pipelines が管理しています。確認が正常に戻ると自動で閉じます。",
           "ledger_title": "期限の台帳: 対応が必要な項目があります", "expired": "期限切れ・故障", "soon": "まもなく期限"},
}


def fingerprint(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:10]


def _t(lang: str) -> dict[str, str]:
    return TEXT.get(lang, TEXT["en"])


def render_diagnosis(d: Diagnosis | None, lang: str) -> str:
    t = _t(lang)
    if d is None:
        return f"### {t['diag']}\n{t['none']}\n"
    out = [f"### {t['diag']}"]
    if d.error and not d.summary:
        out.append(f"{t['none']} — {d.error}")
        return "\n".join(out) + "\n"
    out.append(f"**{t['cause']}:** {d.label(lang)}  \n**{t['confidence']}:** {d.confidence}")
    if d.summary:
        out += ["", d.summary]
    if d.human_steps:
        out += ["", f"**{t['steps']}:**"] + [f"{i}. {s}" for i, s in enumerate(d.human_steps, 1)]
    if d.evidence:
        out += ["", f"<details><summary>{t['evidence']}</summary>", ""] + [f"- `{e.replace('`', "'")}`" for e in d.evidence] + ["", "</details>"]
    if d.run_url:
        out += ["", f"{t['run']}: {d.run_url}"]
    meta = [f"model {d.model}"] if d.model else []
    if d.usage:
        meta.append(f"tokens in/out {d.usage.get('input_tokens', 0)}/{d.usage.get('output_tokens', 0)}")
    if d.redactions:
        meta.append("redacted " + ", ".join(f"{k}×{v}" for k, v in sorted(d.redactions.items())))
    if meta:
        out += ["", f"<sub>{' · '.join(meta)}</sub>"]
    return "\n".join(out) + "\n"


def render_check(result: dict[str, Any], lang: str) -> str:
    t = _t(lang)
    rows = [
        f"| {t['status']} | `{result['status']}` / `{result.get('category') or '-'}` |",
        f"| {t['why']} | {result.get('message', '')} |",
        f"| {t['newest']} | {result.get('latest_at') or '-'} ({result.get('age_hours') if result.get('age_hours') is not None else '-'}h) |",
        f"| {t['threshold']} | {result.get('max_age_hours')}h |",
    ]
    return f"| | |\n|---|---|\n" + "\n".join(rows) + "\n"


def _find(open_issues: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    mark = MARK.format(key=key)
    return next((i for i in open_issues if mark in (i.get("body") or "")), None)


def _last_state(gh, issue: dict[str, Any]) -> tuple[str | None, datetime | None]:
    state, when = None, None
    m = _STATE_RE.search(issue.get("body") or "")
    if m:
        state = m.group(1)
        when = _parse(issue.get("created_at"))
    for c in gh.comments(issue["number"]):
        m = _STATE_RE.search(c.get("body") or "")
        if m:
            state, when = m.group(1), _parse(c.get("created_at"))
    return state, when


def _parse(stamp: str | None) -> datetime | None:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")) if stamp else None


def sync_problem(gh, *, key: str, title: str, abnormal: bool, state: str, body: str, issues_cfg, lang: str,
                 open_issues: list[dict[str, Any]], now: datetime,
                 diagnose_fn: Callable[[], Diagnosis | None] | None = None, diagnose_when: str = "new_or_changed") -> dict[str, Any]:
    """Returns {"action": opened|commented|skipped|closed|none, "number": ..., "diagnosis": ...}."""
    t = _t(lang)
    issue = _find(open_issues, key)
    if not abnormal:
        if not issue:
            return {"action": "none"}
        gh.comment(issue["number"], f"### ✅ {t['recovered']}\n\n{body}\n{STATE.format(state='ok')}")
        gh.close(issue["number"])
        return {"action": "closed", "number": issue["number"]}

    if not issue:
        diag = diagnose_fn() if diagnose_fn else None
        full = f"{MARK.format(key=key)}\n### ⚠️ {t['opened']}\n\n{body}\n{render_diagnosis(diag, lang) if diagnose_fn else ''}\n---\n<sub>{t['footer']}</sub>\n{STATE.format(state=state)}"
        created = gh.create_issue(title, full, [issues_cfg.label], issues_cfg.assignees)
        return {"action": "opened", "number": created.get("number"), "diagnosis": diag.to_dict() if diag else None}

    prev_state, prev_when = _last_state(gh, issue)
    changed = prev_state != state
    due = prev_when is None or (now - prev_when).total_seconds() / 3600 >= float(issues_cfg.repeat_comment_hours)
    if not changed and not due:
        return {"action": "skipped", "number": issue["number"]}
    diag = diagnose_fn() if diagnose_fn and (changed or diagnose_when == "every_run") else None
    head = f"### 🔁 {t['changed']}" if changed else f"### ⏳ {t['still']}"
    extra = render_diagnosis(diag, lang) if diag else ""
    gh.comment(issue["number"], f"{head}\n\n{body}\n{extra}\n{STATE.format(state=state)}")
    return {"action": "commented", "number": issue["number"], "changed": changed, "diagnosis": diag.to_dict() if diag else None}


def check_state(result: dict[str, Any]) -> str:
    """State fingerprint: category only (message has changing ages, so it is not part of it)."""
    return f"{result.get('status')}:{result.get('category') or '-'}"


def ledger_body(rep, lang: str) -> tuple[str, str]:
    t = _t(lang)
    lines: list[str] = []
    if rep.expired_or_broken:
        lines += [f"**{t['expired']}**", ""] + [
            f"- `{r['id']}` ({r['kind']}, {r['status']}{', ' + r['expires'] if r['expires'] else ''}) — {r['name']}  \n  {r['check']}"
            for r in rep.expired_or_broken] + [""]
    if rep.expiring_soon:
        lines += [f"**{t['soon']}**", ""] + [
            f"- `{r['id']}` — {r['expires']} ({r['days_left']}d) — {r['name']}  \n  {r['check']}" for r in rep.expiring_soon]
    ids = sorted(r["id"] for r in rep.expired_or_broken + rep.expiring_soon)
    return "\n".join(lines) + "\n", fingerprint(*ids)


def sync_all(gh, results: list[dict[str, Any]], ledger_rep, cfg, now: datetime | None = None,
             diagnose_for: Callable[[dict[str, Any]], Diagnosis | None] | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    lang = cfg.diagnose.language
    open_issues = gh.open_issues(cfg.issues.label)
    actions: list[dict[str, Any]] = []
    for r in results:
        abnormal = r["status"] != "ok"
        fn = (lambda r=r: diagnose_for(r)) if (diagnose_for and abnormal) else None
        act = sync_problem(
            gh, key=f"check:{r['name']}", title=f"[quiet-pipelines] {r['name']}: {r.get('category') or 'abnormal'}",
            abnormal=abnormal, state=check_state(r), body=render_check(r, lang), issues_cfg=cfg.issues, lang=lang,
            open_issues=open_issues, now=now, diagnose_fn=fn, diagnose_when=cfg.diagnose.when)
        actions.append({"key": f"check:{r['name']}", **act})
    if ledger_rep is not None:
        body, fp = ledger_body(ledger_rep, lang)
        act = sync_problem(
            gh, key="ledger", title=f"[quiet-pipelines] {_t(lang)['ledger_title']}",
            abnormal=ledger_rep.needs_attention, state=f"ledger:{fp}", body=body, issues_cfg=cfg.issues, lang=lang,
            open_issues=open_issues, now=now)
        actions.append({"key": "ledger", **act})
    return actions
