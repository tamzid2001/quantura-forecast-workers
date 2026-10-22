# Quantura Metaculus bot

The Quantura bot discovers open questions in active public tournaments and indexes
where the account has forecast permission, plus bot-oriented question series such
as MiniBench and AI 2027. It scans every
page, unpacks group questions, handles conditional outcomes separately, and checks
each question's own opening/closing times. Newly eligible competitions are picked
up automatically. Coverage includes Labor Automation, Global Health, US Midterms,
POTUS, Democracy Threat, Horizon Scanning, Respiratory Outlook, AI Pathways, Sagan,
ACX, Cultured Meat, Chinese AI Chips and Climate Tipping Points when active.

Forecast permission and prize eligibility are separate. Metaculus's
[`bot_leaderboard_status` definition](https://github.com/Metaculus/metaculus/blob/main/projects/models.py)
describes `exclude_and_show` as allowing a leaderboard display while excluding
bots from ranks, prizes and medals. It does not revoke forecast permission.
`exclude_and_hide` also affects visibility rather than forecast permission.
The worker retains its registered bot identity and reports both the leaderboard
status and prize eligibility. `include` or `bots_only` with a prize pool is labeled
`subject_to_tournament_rules`, rather than promising eligibility for this account;
other competition-specific entry restrictions still apply. Under the general
[tournament rules](https://www.metaculus.com/tournament-rules/), bots are ineligible
for prizes unless a competition explicitly permits them. Unknown policy states,
closed/future competitions, and accounts without forecast permission are excluded.

## Operation

- Workflow: [Quantura Metaculus Bot](https://github.com/tamzid2001/quantura-forecast-workers/actions/workflows/metaculus-bot.yml).
- Schedule: polling at minutes 0, 15, 30 and 45; forecasting at 7, 22, 37 and 52 of every hour, including weekends. GitHub can delay
  or drop scheduled runs; this is periodic automation, not a continuously running
  process or a guarantee to meet every short deadline.
- Runtime: the existing public `tamzid2001/quantura-forecast-workers` repository.
  The private application workflow remains disabled to prevent duplicate workers
  and avoid the exhausted private Actions allowance.
- Secrets: `METACULUS_TOKEN`, `OPENROUTER_API_KEY` and optional gated-model
  `HF_TOKEN` in the runtime repository’s GitHub Actions Secrets. Never put
  either in source files, browser code, inputs, or artifacts.
- Enable: repository variable `METACULUS_BOT_ENABLED=true`.
- Submission approval: `METACULUS_SUBMISSIONS_APPROVED=true` only for an account
  permitted by Metaculus to submit. It defaults to false, including manual runs.
- Account binding: `METACULUS_BOT_ID` must match the authenticated bot. Recovery
  artifacts are namespaced as `metaculus-checkpoint-bot-<id>-...`; another bot's
  generations and comments are never imported.
- Participation form: `METACULUS_PARTICIPATION_FORM_CONFIRMED=true` after submission.
- Pause: set `METACULUS_BOT_ENABLED=false` or disable the workflow. Existing forecasts
  remain on Metaculus; pausing does not erase them.

Each question receives one forecast. Subsequent runs skip authoritative own
forecast history through `questions/bulk-forecast-read/`; feed metadata alone is
not treated as a submission receipt. An encrypted recovery artifact preserves the
exact generated answer before submission. The private reasoning note contains a
brief explanation, model attribution, real source links and a compact answer hash;
it contains no raw answer JSON, data hashes or duplicated recovery payload.
Uncertain writes are checked against Metaculus; writes aren't blindly retried.
Selected general-purpose free models and MiniMax use JSON mode when supported for endpoint compatibility. The
SDK validates exact question options, numeric units, ordered quantiles and CDFs
before posting; JSON mode does not bypass distribution validation. An accepted
but unusable response is encrypted and not automatically regenerated. A confirmed moderation-only routing result contains no forecast; it is audited and requeued after the model-routing fix. This does not change a usable forecast or select among candidate probabilities.
If a note exists without its exact recovery answer, the worker defers rather than
generating a different prediction or posting another comment.
Metaculus requests are spaced at least five seconds apart. Read retries honor
`Retry-After` in seconds or HTTP-date format. Exhausted quotas defer work instead
of crashing the worker, and a restored cooldown prevents requests before it expires.
Own comments are read once per job, saved abstentions do not consume new-generation
capacity, and the time-series job fetches only the posts selected during discovery.
Pending exact answers are recovered first. New questions in prize-bearing
MiniBench and FutureEval rounds take priority, followed by other tournaments
whose policies permit potential bot prizes, then non-prize practice. Within each
tier, questions closing within two hours take priority. The remaining backlog is balanced across tournaments by processed
question counts, with the earliest deadline first within each tournament. Questions
shared by tournaments receive one forecast and count toward each tournament's
coverage. Selection never depends on forecast probabilities or leaderboard scores.
Each Actions summary shows open questions, previous submissions, new confirmed
submissions and new reasoning notes by tournament, including tournaments with no
open questions. A zero is visible rather than presented as a submission.

Every submitted forecast has an automated private reasoning note. Metaculus
requires bots to leave private comments and publishes FutureEval notes itself at
regular intervals; main-site notes remain private unless it grants permission.
New LLM explanations target 60–100 words. Posted reasoning is capped at 100 words
and 1,000 characters, including old pending generations. Source links are limited
to three retrieved URLs. Distribution validation happens before any comment;
abstentions and invalid forecasts produce no comments. The worker does not publish
promotional or duplicate comments. Comments explain
the forecast; leaderboard scores depend on resolved forecasting performance.
Actions artifacts contain operational summaries, not private reasoning.

The bounded restart policy permits, at most **five new private-note attempts per job** in the hosted profile (the local fallback is two)
and **50 attempts per UTC day** in the hosted profile (the local fallback is 12) are permitted. The encrypted checkpoint reserves
capacity before each network request, including ambiguous writes. These are
Quantura's own limits, not a claim about Metaculus's permitted posting volume.
Unconfirmed note writes are not repeated: if their receipt is still missing after
a restart, the original answer is held for review. A 401/403 during any question
stops the entire batch immediately.

### Moderation restrictions

Submissions were paused on October 10, 2026 after moderation restrictions. On
October 21, the owner confirmed Metaculus administrators had re-enabled the bots.
A read-only identity check confirmed the original registered **Quantura** account
(`310211`) is active with `api_forecasting_access=enabled`. The runtime binds to
that exact ID and uses its own authoritative forecast history. Other bot identities
and their encrypted checkpoints are not imported. A later 401/403 stops the batch
for operator review. Private comments, deduplication, conservative posting limits
and the participation-form requirement remain in force. The
[community guidelines](https://www.metaculus.com/help/guidelines/) govern conduct.

The unscored **bot-testing-area** supports a manual smoke test. Validate there
before running `scope=competitions, submit=true`. Follow the
[competition rules](https://www.metaculus.com/notebooks/38928/ai-benchmark-resources/):
no human adjustment of individual forecasts, no rerunning a forecast because its
probability is disliked, and no tuning against previews of open tournament forecasts.

## Free models and capacity

The router checks the current OpenRouter catalog and permits only explicitly free
general-purpose entries with zero listed prices. It pins Gemma 4 31B IT (free), with Nemotron 3 Ultra (free) as the catalog-selection alternative. The unrestricted free pool is excluded because it can route to moderation-only classifiers. It checks the key's free daily quota before each
generation. It sends no paid search plugins, tool calls, or paid fallback models.
If quota is exhausted or the service is unavailable, work is deferred. As configured
on October 10, 2026, the account allows **50 free model requests/day**; the actual
API quota is authoritative and may change. The default hosted batch is five new LLM
questions and up to two numerical time-series questions per run. A UTC daily
private-note limit is checked before new inference so exhausted posting capacity
does not waste free model requests. Invalid answers
and abstentions are reported. Complete coverage cannot be promised when free
capacity is lower than the question backlog or questions close before processing.

Binary and multiple-choice probabilities are validated. Numeric, discrete and date
forecasts use the official `forecasting-tools` distribution conversion, including
logarithmic scales, open/closed bounds and the question's exact CDF size. Numeric
answers use original units; dates use UTC timestamps.

## Quantura time-series route

The time-series job reuses `ensemble_forecasting.worker.execute_job` and the
production screener profile: **Prophet, Toto 2.0 4m, Granite, Chronos, TimesFM,
Timestral and t0-gamma**. Model checkpoint revisions are pinned by the shared
registry. It uses up to 500 genuine observations and seven quantiles: P01, P10,
P25, P50, P75, P90 and P99. Each estimate targets the **date named in the resolution
criteria**, not the later date when Metaculus plans to resolve the question.

Weights use `rolling_loss_selection_v2`: four historical calibration windows,
inner validation over loss-weight candidates, and a separate audit window.
Per-quantile support is respected; unsupported tails are not invented. All seven
models and completed weight optimization are required. Missing licensing/access,
failed inference or equal-weight fallback caused by failed calibration defers the
question. An equal weighting deliberately selected by successful historical
optimization is valid. This configuration does not establish superior Metaculus
accuracy; performance is measured only after questions resolve.

The first automatic source adapter covers Silver Bulletin's publicly downloadable
Trump approval series when that exact source and a clear target date are named in
the question's criteria. Approval and net approval are distinguished. This public
series is fetched from its current Datawrapper chart; archived chart versions,
future rows, stale data and duplicate/out-of-order dates are rejected. Paywalled
material is not accessed. General event questions are labeled `free_llm`; they are
not presented as time-series ensemble forecasts.

Other exact public CSV mappings can be reviewed in `metaculus_bot/series_registry.json`:

```json
{
  "question_id": 12345,
  "resolution_sha256": "SHA256 of the exact resolution criteria",
  "kind": "public_csv",
  "url": "https://example.org/public-series.csv",
  "time_column": "date",
  "value_column": "value",
  "unit": "original resolution units",
  "frequency": "1D",
  "calendar": "NONE",
  "target_date": "2027-01-01",
  "max_history_age_days": 3
}
```

Match the source definition, units, aggregation period and target date before
adding a mapping. An asset's market price is not a substitute for its revenue,
earnings, opinion polls, or any unrelated KPI. Arbitrary untrusted question URLs
cannot access internal hosts, credentials, metadata endpoints or authenticated
provider sessions.

No Firestore or Cloud Storage is used. Confirmed submission history lives in
Metaculus. An encrypted seven-day GitHub recovery artifact preserves pending
generations, abstentions and API cooldowns between runs. Exact answers remain in
recovery state until their forecasts are confirmed; private notes only identify
their answer hashes. Its
encryption key is derived from the secret Metaculus token with a dedicated context;
token rotation requires draining pending generations before changing the token.
Recovery lookup is scoped to this workflow’s recent runs and the current bot ID,
so unrelated screener artifacts cannot hide the ledger. Restoring or decrypting
recovery state must succeed before new work starts.
Operational summary artifacts contain no private forecast values. Existing paused
Kalshi traders and storage-purge controls are independent and remain paused.

## Local tests

```bash
python -m pip install --require-hashes -r metaculus_bot/requirements.lock
python -m pytest -q metaculus_bot/tests
python -m metaculus_bot.runner --scope test --max-questions 1
# Explicitly submit to the unscored testing area:
python -m metaculus_bot.runner --scope test --submit --max-questions 1 --checkpoint artifacts/metaculus-checkpoint.enc
```

Time-series inference uses a separate virtual environment with the existing
`requirements-ensemble-forecast.lock`; the SDK environment cannot replace its
locked numerical/model dependencies. Gated model weights are never published to a
public Actions cache. Operational Actions logs show discovered questions, coverage,
submitted/deferred/abstained counts, engine and submission URLs. Forecast outcomes
and accuracy are only knowable after the questions resolve.

## Connect a free AI provider

OpenRouter is connected for this deployment. The `auto` provider also prefers
Vercel AI Gateway’s exact `minimax/minimax-m3.1-flash` model while its MiniMax
endpoint is verified free. The route checks both catalog and endpoint prices
before every generation, permits only provider `minimax`, and configures no model
fallbacks, tools or paid search. The announced promotion ends October 22, 2026.
The live catalogs still reported zero model and MiniMax-provider prices on
October 22. Quantura requires both checks before every request and disables this
route immediately when either price becomes nonzero, with an outer cutoff of
**00:00 UTC on October 23**. The provider may end the offer earlier. It then selects
a verified free OpenRouter model before generating a new answer. Uncertain or
accepted generations are never rerun through another provider.

To connect your own OpenRouter key:

1. Create an OpenRouter account and an API key at https://openrouter.ai/keys.
2. Store it in the **runtime** repository, never in code or a workflow input:

   ```bash
   gh secret set OPENROUTER_API_KEY --repo tamzid2001/quantura-forecast-workers
   ```

   Paste the key at the CLI prompt. The bot chooses a whitelisted general-purpose free model only after
   checking current catalog prices; verified zero-price alternatives are allowed.
   No paid fallback or paid search plugin is configured.
3. Keep `METACULUS_BOT_ID=310211` for the authorized Quantura account. The
   participation and submission approvals are account-specific. Use the
   workflow’s `test` scope before a `competitions` submission after code changes.
4. Enable `METACULUS_BOT_ENABLED=true` only on the runtime repository, then run
   `metaculus-bot.yml` with `scope=competitions`, `submit=true`, `max_questions=5`.
   The schedule checks for work every 15 minutes. Do not enable a second copy.

OpenRouter’s current free limits are 20 requests/minute and 50/day for an account
without qualifying purchased credits; the authenticated key’s quota is checked
before every generation. A free LLM key does not supply time-series model compute:
the seven local adapters execute on standard GitHub-hosted public runners, subject
to their available CPU, memory, download access and job-duration limits.

See [OpenRouter free models](https://openrouter.ai/openrouter/free) and
[rate limits](https://openrouter.ai/docs/api/reference/limits). Confirmed forecasts,
private-note receipts, abstentions, rate limits and coverage by tournament are
reported separately. A discovered tournament or generated answer is not counted
as a submitted forecast. Some competitions do not accept bots or offer them prize
eligibility; the worker does not bypass those restrictions.

### Optional Vercel AI Gateway connection

Create a Gateway key in the Quantura Vercel team, then store it securely:

```bash
gh secret set AI_GATEWAY_API_KEY --repo tamzid2001/quantura-forecast-workers
gh variable set METACULUS_LLM_PROVIDER --body auto --repo tamzid2001/quantura-forecast-workers
```

`auto` prefers the verified promotional MiniMax route and keeps OpenRouter as the
free option after expiry. `vercel` alone defers when the promotion expires or
prices are not zero. `openrouter` uses only the verified general-purpose free model whitelist. The dedicated
Gateway key created for this deployment expires after seven days and has the
CLI’s smallest total quota ($1); this is a ceiling, not a purchase or permission
to make paid requests. Price/expiry guards still apply to every inference.

[Vercel’s promotion notice](https://vercel.com/changelog/minimax-m3-1-flash-now-available-on-ai-gateway-for-free)
explains that free pricing is temporary and limited to the MiniMax provider.

## Current deployment and scoring rules

The initial deployment used `METACULUS_DRY_RUN=true` for readiness review. The
owner subsequently explicitly instructed immediate live submission on October 21,
so the runtime is now **live** with `METACULUS_DRY_RUN=false`. The switch remains
available as an immediate paper-only control; workflow inputs cannot bypass it.
Metaculus permission approval and this owner-controlled gate remain separate.
The first retrospective readiness scan returned no usable resolutions from the
API; no Brier or peer score is claimed from that empty scan.

The official FutureEval resources specify **one forecast per question** for
MiniBench and the bot-only seasonal tournament. These are not repeatedly refreshed
near close. Fresh questions are ordered by close time and discovered on every poll.
Market Pulse expects updates under its own rules; this conservative version sends
initial forecasts only there as well. It does not promise complete Market Pulse
participation or implement repeated updates yet. Tournament-specific rules take
precedence over a generic “last forecast wins” assumption.

Poll snapshots carry question/post IDs and deadlines, expire after 30 minutes,
and are rechecked against the current question before inference/submission.
Only forecast workers write recovery state. A 401/403 permanently marks that
checkpoint for operator review so the next tick cannot repeat a restricted write.
Each processed forecast receives an encrypted per-run audit record, including
its exact answer and status; audit artifacts retain for 90 days. Public repository
activity records contain only timestamps, run IDs, mode and aggregate counters.
No API key, internal prompt, private answer or reasoning text is committed.

### cron-job.org backup watchdog

Native GitHub cron runs both jobs already. The optional external watchdog starts
only a missing/stale worker (12 minutes since its last start) and skips any queued
or active run. It does not cancel a worker or bypass dry-run/approval controls.
Use one cron-job.org job every 15 minutes with these settings:

- Method: `POST`
- URL: `https://api.github.com/repos/tamzid2001/quantura-forecast-workers/actions/workflows/metaculus-watchdog.yml/dispatches`
- Headers:
  - `Authorization: Bearer <YOUR_GITHUB_FINE_GRAINED_PAT>`
  - `Accept: application/vnd.github+json`
  - `Content-Type: application/json`
  - `X-GitHub-Api-Version: 2022-11-28`
- Body: `{"ref":"main","inputs":{"target":"both"}}`
- Expected response: `204 No Content`; that means GitHub accepted the watchdog,
  not that forecasting or submission finished successfully.

The GitHub PAT should be limited to `tamzid2001/quantura-forecast-workers` with
**Actions: read and write**. Do not put the Metaculus or Gateway key in these
headers. To use two cron jobs instead, use the same URL with `target:"poll"` and
`target:"forecast"`; offset the forecast watchdog by seven minutes. Keep the token
only in cron-job.org’s protected headers, never in the URL or a public text file.

Inspect the Actions pages and `status/metaculus.json` for actual status. GitHub
scheduled runs can be delayed or dropped; native cron plus a backup watchdog is
periodic automation, not a guaranteed continuously running service.

Polling balances the remaining backlog across tournaments while prioritizing imminent deadlines. Forecast batches fetch only a bounded subset of queued posts (three times the batch size, capped at 100), avoiding a 100-post metadata wait before the first small submission batch.
