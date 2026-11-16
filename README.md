# quiet-pipelines

**Catch pipelines that keep running but stop producing — and the keys that are about to expire.**

A scheduled job can be "green" for months while publishing nothing: an OAuth token was
revoked, a quota ran out, a free API changed, a self-healing step silently gave up.
quiet-pipelines checks the **outcome** (when did something new actually appear?), keeps a
**ledger** of everything that can expire, and when something is wrong it opens a GitHub
Issue — with an optional Claude diagnosis of the latest failed run's logs.

- One Issue per problem. Still broken → a comment (at most one reminder per day). Fixed → closed automatically.
- Uses the built-in `GITHUB_TOKEN`, so it keeps working even when the PAT you are watching expires.
- Secrets are masked before any log leaves the runner. Low-confidence diagnoses are reported as **unknown**, never as a guess.

## Install in 5 minutes

**1. Add `quiet-pipelines.yml`** to the root of the repository you want to watch
(full example: [`examples/quiet-pipelines.yml`](examples/quiet-pipelines.yml)):

```yaml
version: 1
defaults:
  max_age_hours: 36
checks:
  - name: registry-file              # a file your pipeline commits on success
    type: repo_file_freshness
    path: output/registry.jsonl
    workflows: [daily_post.yml]      # where to look for failed runs
ledger:
  items:
    - id: github_pat
      kind: pat
      name: GitHub Secret GH_PAT
      expires: 2027-02-15
      check: GitHub > Settings > Developer settings > Personal access tokens
```

**2. Add `.github/workflows/quiet-pipelines.yml`** ([`examples/workflow.yml`](examples/workflow.yml)):

```yaml
name: quiet-pipelines
on:
  schedule: [{cron: "17 */6 * * *"}]
  workflow_dispatch:
permissions: {contents: read, actions: read, issues: write}
jobs:
  watch:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: OWNER/quiet-pipelines@v0.1.0
        with:
          anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}   # optional
```

**3. (Optional) add the `ANTHROPIC_API_KEY` secret** for diagnoses. Without it everything
else works; Issues just have no "likely cause" section.

Run it once from the Actions tab (`workflow_dispatch`) with `dry-run: "true"` to see what it would do.

## Checks

Every check finds the timestamp of the newest output and compares it with `max_age_hours`.

| type | answers | key params |
|---|---|---|
| `youtube_latest_upload` | newest **public** video on a channel | `auth: oauth` + `token_json_env` (authorized-user token JSON in a secret) **or** `auth: api_key` + `api_key_env` + `channel_id`; `expect_channel_name_contains` |
| `rss_freshness` | newest item in an RSS/Atom feed | `url`, `headers_env` |
| `http_freshness` | an endpoint's own "updated" time | `url`, `source: last_modified \| json_field \| regex`, `field`, `pattern`, `headers_env` |
| `repo_file_freshness` | last commit touching a file | `path`, `repo`, `branch`, `ignore_authors` |

Results are `ok` or `abnormal` with a category: `no_output`, `auth`, `quota`, `unreachable`, `config`, `api_error`.
Secrets are referenced **by env var name only**; pass them to the action step with `env:`.

Add your own check:

```python
from quiet_pipelines.checks import BaseCheck, register

@register
class QueueDrained(BaseCheck):
    type_name = "queue_drained"
    required = ("queue_url",)
    def latest_output(self):          # -> (datetime_of_newest_output, details_dict)
        ...
```

## Expiry ledger

List what can expire — tokens per account, PATs, API keys, model names, free tiers — with
**names and locations only**. Items with `expires` within `warn_within_days` (default 30),
or with `status: expired | broken`, open a single "expiry ledger" Issue. Items with
`expires: null` are listed by `quiet-pipelines ledger` together with their `check:` hint.

## Diagnosis (optional, Claude)

When a check turns abnormal (or changes category), quiet-pipelines:

1. finds the newest failed run of the check's `workflows` in the last `lookback_hours`,
2. downloads the failed jobs' logs (kept: error-like lines + the last part, up to `max_log_chars`),
3. **masks secrets**: values of env vars named like `*_TOKEN`, `*_KEY`, `*_SECRET`, … (including fields of JSON secrets), known token formats (GitHub, Anthropic, OpenAI, Google, AWS, Slack, Hugging Face, JWT, private keys), credentials in URLs, `Authorization` headers, `password=`-style assignments and long mixed-case blobs,
4. asks Claude (`claude-opus-5-5` by default, structured JSON output) for one of
   **expired_or_auth · external_change · quota · code_bug · unknown**, a confidence, quoted evidence and numbered steps for a human.

If the model's confidence is low, the category is forced to **unknown** in code. The model's
answer is masked again before it is posted. Set `diagnose.language: ja` for Japanese.

Log text is sent to the Anthropic API. If that is not acceptable, set `diagnose.enabled: false`.

## Run locally

```bash
pip install .
quiet-pipelines validate            # config sanity
quiet-pipelines check               # outcome checks only (needs the env vars they reference)
quiet-pipelines ledger              # expiry ledger only
GITHUB_REPOSITORY=owner/repo GITHUB_TOKEN=... quiet-pipelines run --dry-run
```

`run` exit codes: `0` (also when abnormal — the Issue is the signal), `1` with `--fail-on-abnormal`, `2` config error.

## Development

```bash
pip install -e ".[dev]" && pytest      # unit tests; no network, no API calls
```

## Limits

- It can't notice that GitHub Actions itself stopped running (the watchdog runs there too). Pair it with something outside, e.g. a weekly local cron of `quiet-pipelines check`.
- Redaction is best effort. GitHub already masks registered secrets in logs; this is a second layer, not a guarantee.
- Diagnosis reads only the newest failed run in the window, and only the configured workflows.
