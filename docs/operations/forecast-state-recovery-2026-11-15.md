# Forecast state recovery — November 15, 2026

## Incident and scope

The Kalshi perpetual screener's four publication shards failed with
`PUBLIC_SNAPSHOT_NOT_PUBLISHED` (run 43765592605). The full stock screener
completed inference and aggregation but failed while packaging its publication
because the previous trusted snapshot was missing (run 43707051055).
The research workflow failed with `RESEARCH_REGISTRY_MISSING_CANNOT_SILENTLY_RESTART`
(run 43722625797). These errors correctly prevented silent history resets.

The repair restores reviewed source publications, without changing forecast
models, inventing trades, or treating restoration as new inference. Backend
schedules recovered on run 43755203288. Metaculus, pregame forecasts, homepage
forecasts and the separate forward limit-paper workflow remain enabled.

## Verified inputs

Recovery release: `tamzid2001/quantura-forecast-workers`, tag
`forecast-state-recovery-20261115`.

Manifest SHA-256:
`786728c49306d5d9ee188ff0da159bf11f32d8ef5c7718b7057caab49c3499ea`.

The manifest records payload hashes, byte lengths and source receipts:

- Stocks: 3,612 items from the genuine October 30 publication retained in
  `screener-latest`. Its original publication timestamp is retained.
- Four perpetual shards: original `screener-bootstrap-seed` release assets.
- Four games shards: November 15 run 43764874799, validated against the
  official workflow/main provenance and GitHub artifact archive digests.
  Healthy existing feeds are skipped rather than replaced.
- Research: the complete original `registration` object inside `prospective.json`
  from successful run 42376408210. The preserved report ZIP SHA-256 matches the
  original GitHub upload log:
  `158f605311c950df3faba8f0d4ee1d147ce18cf391f0a53f441949e29db2e0b8`.
  Registration starts October 28, ends December 27, and permits final review
  December 29. No dates, design hashes or amendments are restarted.

Only previously public curated results and the experiment registration belong
in this release. Do not upload private CSVs, credentials or customer inputs.

## Controls and commands

`market_research.recovery_seed` validates a pinned manifest, bounded payload
size, exact filename, payload hash, original snapshot schema or frozen research
window, and conflicting destinations. It rejects symlinked payloads and copies
validated bytes atomically. It reports `new_forecasts: 0`.

The manual bootstrap probes existing trusted feeds first. Provider/API failures
and corruption remain errors; only explicit missing-publication errors permit
restoration. Restored artifacts retain the canonical names already trusted by
the website and worker, with 90-day retention. No trust allowlist is expanded.

```sh
gh workflow run screener-artifact-bootstrap.yml --repo tamzid2001/quantura-forecast-workers --ref main
gh workflow run sports-strategy-research.yml --repo tamzid2001/quantura-forecast-workers --ref main -f restore_registry=true
# After verifying restoration succeeded, run analysis separately:
gh workflow run sports-strategy-research.yml --repo tamzid2001/quantura-forecast-workers --ref main -f restore_registry=false
```

The research restore-only run refuses to overwrite an existing trusted registry.
It uploads the original registration through the official research workflow;
it does not claim a new evaluated research report. A subsequent normal run
collects genuine available receipts and reports unavailable/stale sources.

Resume failed stock publication using its completed inference artifacts where
possible. Inspect newer active runs first; do not replace a newer completed
publication with an older same-session run or rerun inference unnecessarily.

## Hourly replay remains paused

`games-p01-paper.yml` was disabled explicitly because its latest cumulative
checkpoint is missing. Its last successful run was 42486797730. The known
older backup (run 42442439180) contains 171 trades; later successful logs show
172. Substituting the older checkpoint would omit at least one genuine trade.
Do not reset this ledger or reconstruct its missing trade from summary logs.
Restore the exact latest checkpoint before re-enabling this workflow.

The independent `games-p01-limit-paper.yml` remains enabled. Neither restoration
nor research enables live exchange orders or changes billing.

## Executed validation before rollout

- Canonical source: 84 tests and 53 subtests passed across recovery and sports
  research modules.
- Public runtime: 87 tests and 59 subtests passed across the same scope.
- Recovery and public-snapshot tests: 22 passed; nonempty Cobertura/LCOV reports
  were produced. CI separately enforces recovery line coverage at least 90%
  and branch coverage at least 70%.
- `actionlint` passed for all three changed workflows in source and runtime.
- All ten genuine recovery assets passed the same helper validation used in CI.

The runtime contains newer independent sports changes. Apply this repair as an
incremental export, preserving those files. Its manifest records the source
commit and targeted file hashes rather than claiming a fresh full export.

## Rollout receipts

Runtime PR 29 merged as `fd12017597a4510b8059f51dd0b5c046d2e402cb`.
Recovery release hashes and lengths were checked against all eleven uploaded
GitHub release assets. Snapshot restore run 43775669028 succeeded: five missing
feeds restored; four healthy games feeds preserved.

Registry restoration run 43775671428 succeeded. Its downloaded `registry.json`
matched the original byte hash
`dda49739793d24a523128335062d1b9dd32d886e269bedc7823e082ea35941b8`.
Normal analysis run 43775769012 then revealed a separate report compatibility
issue: the current official limit report includes `depth-shadow-stats.json`
and `depth-shadow-evidence.json`, and the historical report includes
`catalog-recovery.json`. The research archive allowlist now permits those exact
diagnostic filenames, retaining size/path limits and rejecting other files.
Diagnostics are preserved as evidence; only the original canonical ledgers
enter research trade normalization. A regression test exercises both actual
archive layouts, missing-paper reporting, and rejection of unknown sidecars.

The recovery Cobertura and LCOV files are retained alongside the existing
coverage artifact so CI evidence remains downloadable.

Follow-up validation: 85 tests / 53 subtests passed in canonical source and
88 tests / 59 subtests passed in the runtime, including the sidecar regression.
Combined recovery/research coverage exceeded the enforced 90% threshold.
