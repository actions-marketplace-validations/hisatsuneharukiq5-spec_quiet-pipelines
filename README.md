# quiet-pipelines

**A watchdog that notices, from the outside, when an unattended automation has quietly stopped.**

## Why this exists

My YouTube automation ran on GitHub Actions for about six months with every run green and
zero videos published. An OAuth token had expired, the job caught the error and exited
cleanly, and nothing looked wrong until I checked the channel by hand.
quiet-pipelines checks whether the output actually appeared, not whether the job said it succeeded.

## What it watches

- **Outcome checks.** Did something new actually appear? Each check finds the newest output
  and compares its age with `max_age_hours`.

  | type | looks at |
  |---|---|
  | `youtube_latest_upload` | newest **public** video on a channel (OAuth token or API key) |
  | `rss_freshness` | newest item in an RSS/Atom feed |
  | `http_freshness` | an endpoint's own "updated" time (`Last-Modified`, a JSON field, or a regex) |
  | `repo_file_freshness` | last commit that touched a file (e.g. a registry your pipeline commits) |

- **Log inspection.**
  - When a check turns abnormal, it reads the newest failed run of the workflows you list.
    That includes failed earlier attempts of runs that were later re-run green.
  - It can also ask Claude for a diagnosis: one of `expired_or_auth`, `external_change`,
    `quota`, `code_bug` or `unknown`, with quoted evidence and steps for a human.
  - Low-confidence answers are forced to `unknown` in code, so a guess is never presented as a cause.
  - Separately, it scans the logs of **successful** runs for failures that were swallowed
    (`git push || true`, `continue-on-error`): 401/403, `invalid_grant`, `quotaExceeded`,
    tracebacks and the like.
- **Expiry ledger.**
  - You list everything that can expire: tokens, PATs, API keys, model names and free tiers.
    You record names and locations only, never values.
  - Items expiring within `warn_within_days`, or marked `expired` / `broken`, open one ledger Issue.
- **Issues without noise.**
  - Each problem gets one Issue, found again later by a hidden key.
  - While a problem lasts, the Issue gets at most one reminder comment per `repeat_comment_hours`.
  - When the check is healthy again, the Issue is **closed automatically**.

## What it cannot see

- **The content of the output.** It knows *that* a new video, post or file appeared, not
  whether it is right. A video that was published without its custom thumbnail looks
  healthy to quiet-pipelines. I had exactly that case, without anyone noticing.
- **The whole of a huge log.** The diagnosis sends an excerpt of at most `max_log_chars`
  (60,000 by default): the error-like lines from the beginning plus the end of the log. If the
  real error sits in the middle of a 30 MB test log, the diagnosis will not see it, and it
  will answer `unknown`.
- Also out of reach:
  - A cause that lives in a *different* run, e.g. a job that timed out waiting for another CI run.
  - GitHub Actions itself not running, because the watchdog runs there too. Pair it with
    something outside, e.g. a local cron of `quiet-pipelines check`.

## How accurate the diagnosis is

These are measured numbers, not claims.

| set | result |
|---|---|
| 3 real failures from my own pipelines | 3 of 3 classified correctly |
| 7 real failures from other people's public repositories | 3 correct, 1 partly correct, **0 wrong**, 3 answered `unknown` |

The first set, one line each:

- YouTube upload job, OAuth refresh token revoked (`invalid_grant`): `expired_or_auth`. Fixed by re-authorizing.
- Video job crashing at start because a config file was never committed: `code_bug`. Fixed by committing it.
- Analysis job whose `git push` and `gh secret set` got 403 from a PAT without enough permissions: `expired_or_auth`. Fixed by recreating the PAT.

These are the cases I looked at while building the prompt, so treat them as in-sample. The second set is the fairer measure.

About the second set:

- **How the correct answers were set:** for each case, from the commit that fixed it, written
  down before the diagnosis ran.
- **The "partly correct" case:** the steps matched the fix, but the category did not. The
  upstream API changed a field type, and the model called it `code_bug`. The prompt now says
  that a change in upstream data is `external_change`. Re-running that one case once with the
  new prompt gave `external_change` (medium confidence), with the same fix steps. That is one
  run of the case the rule was written for, so the table above keeps the original result.
- **Why the three `unknown`s:** each time the deciding evidence was not in the log excerpt.
  There was a log too large to fit, a cause in another run, and a tool that printed no details.
- **This set is biased toward easy cases.** Cases were only usable if a fixing commit made the
  correct answer clear, so clear-cut failures were favoured. For the same reason it contains
  **no expired-credential and no quota cases**: those are fixed by rotating a secret or
  waiting, which leaves no commit to compare against. Expect more `unknown` answers in
  real use than this table suggests.

## What you need

- **`GITHUB_TOKEN`:** the built-in token is enough, with these permissions:
  - `contents: read`
  - `actions: read` (to read run logs)
  - `issues: write` (to open, comment on and close Issues)

  No personal access token is needed, so the watchdog keeps working when the PAT you are
  watching expires.
- **`ANTHROPIC_API_KEY`:** only if you want the diagnosis. Without it, everything else works
  and Issues simply have no "likely cause" section.
- The secrets your checks reference (e.g. a YouTube OAuth token JSON). They are passed **by
  env var name only**.

## What happens to your logs

To diagnose a failure, quiet-pipelines sends **part of the failed job's log** to the
Anthropic API: the excerpt described above, together with the check result. Secrets are
masked twice before anything is sent:

1. **GitHub itself** replaces the values of registered secrets with `***` in run logs.
2. **quiet-pipelines** then masks again:
   - the exact values of every environment variable whose name looks secret (`*_TOKEN`,
     `*_KEY`, `*_SECRET`, …), including the string fields of JSON-valued secrets;
   - known token formats (GitHub, Anthropic, OpenAI, Google, AWS, Slack, Hugging Face, JWTs,
     private keys);
   - credentials in URLs, `Authorization` headers and `password=`-style assignments;
   - long mixed-case blobs.

The model's answer, and any error text, is masked again before it is written into an Issue.
Redaction is best effort, not a guarantee. If sending log text to an API is not acceptable
for you, set `diagnose.enabled: false`.

Cost is about $0.08 per diagnosis on `claude-sonnet-5-5` with a full 60,000-character
excerpt. A diagnosis runs only when an Issue opens or its category changes.

## Example setup

`quiet-pipelines.yml` in the repository you want to watch (full version:
[`examples/quiet-pipelines.yml`](examples/quiet-pipelines.yml)):

```yaml
version: 1
defaults:
  max_age_hours: 36                    # abnormal when the newest output is older than this
checks:
  - name: youtube-channel
    type: youtube_latest_upload
    auth: oauth
    token_json_env: YT_TOKEN_JSON      # env var holding the authorized-user token JSON
    expect_channel_name_contains: My Channel
    workflows: [daily_post.yml]        # where to look for failed runs
ledger:
  items:
    - id: github_pat
      kind: pat
      name: GitHub Secret GH_PAT
      expires: 2027-02-15
      check: GitHub > Settings > Developer settings > Personal access tokens
diagnose:
  model: claude-sonnet-5-5
```

`.github/workflows/quiet-pipelines.yml` ([`examples/workflow.yml`](examples/workflow.yml)):

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
        env:
          YT_TOKEN_JSON: ${{ secrets.YT_TOKEN_JSON }}
        with:
          anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}   # optional
```

Start with a manual run with `dry-run: "true"`: it prints what it would do, opens no Issues
and calls no API.

Locally:

```bash
pip install .
quiet-pipelines validate      # check the config
quiet-pipelines check         # outcome checks only
quiet-pipelines ledger        # expiry ledger only
GITHUB_REPOSITORY=owner/repo GITHUB_TOKEN=... quiet-pipelines run --dry-run
```

## License

MIT. See [LICENSE](LICENSE).

## Reproducing the evals

```bash
pip install -e ".[dev]" && pytest          # unit tests: no network, no API calls
ANTHROPIC_API_KEY=... python evals/unknown_cases.py --models claude-sonnet-5-5 --budget 0.30
```

`unknown_cases.py` uses synthetic logs and costs about $0.03-0.06. The real-failure sets above are not in this repository.
