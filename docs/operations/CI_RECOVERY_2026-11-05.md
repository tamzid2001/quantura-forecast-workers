# November 5 worker reliability repairs

These changes repair software control paths. They do not restore a disabled hosting account, place orders, flush billing manually, or replace missing historical observations.

## Verified failure causes

- Full-universe scan run `42136146339`: HTTP 402 from the existing Quantura market-data gateway. All 32 chunks failed before producing checkpoints; the existing coverage gate correctly preserved the prior validated publication.
- Billing run `42134709336`: `METERED_USAGE_HTTP_402`. The backend remains unavailable; changing a workflow cannot clear the hosting-account block.
- Sports review run `42048621559`: `PARTIAL_SETTLEMENT_EVIDENCE`. The historical source includes verified final payouts whose venue does not expose native settlement-credit timestamps.
- Metaculus runs `42080084115` / `42060670335`: GitHub Contents PUT / workflow-dispatch POST failed, but the old subprocess wrapper hid the HTTP status. The exact original statuses were not logged, so a conflict or transient error is a hypothesis rather than a proven original cause.
- The separate perpetual screener fails at Toto inference (`MODEL_INFERENCE_FAILED`), not the website health check. It remains independent of Vercel. A gated CPU SDK diagnostic uses synthetic fixtures and preserves sanitized failure stages without publishing forecasts.

## Repairs

`scripts/service_preflight.py` performs one bounded, read-only HEAD against the same fixed gateway. It follows no redirects, sends no credentials or request body, and logs only status plus allowlisted platform error codes. Full-universe and homepage preparation call it before model-worker fan-out; billing calls it before reading its credential or submitting usage. A failed precondition remains a failed job, with downstream work skipped. HTTP 200 is only an availability precondition, not proof of a successful forecast or billing delivery.

Historical research accepts payout-only resolution **only** for the verified retrospective cohort, with a bounded observation receipt and a checked-at timestamp validated against the source receipt. Native credit time remains `None`; market close is never substituted. Such rows contribute to eventual payout P&L, but cannot enter time-purged training. Realized drawdown and settlement-ordered streaks become unavailable when credit timing is incomplete; entry-order outcome drawdown is separately labeled. Forward-paper and limit inputs retain strict paired settlement evidence. The frozen prospective registration and automatic-live-activation prohibition are preserved.

The GitHub helper now reports sanitized HTTP status/method, bounds read retries and subprocess waits, and never blindly retries writes. Heartbeats refetch the SHA only after confirmed conflicts and cannot overwrite newer activity with an older worker's record. Dispatch failures stay visible; later watchdog ticks inspect existing runs before trying again. This does not change question forecasts, private reasoning requirements, free-provider policy or submission receipts.

The duplicate root research test copies were consolidated into the canonical `market_research/tests/` files, retaining the additional frozen-registration assertion. The research workflow now runs that canonical suite.

## Validation and remaining blocks

- Local research validation: 68 tests and 59 subtests passed, with real Cobertura above the existing 90% combined coverage requirement. A complete read-only replay against current retained source artifacts succeeded with the original registration SHA preserved. It still reports stale paper data and incomplete historical replay.
- Local Metaculus validation: 286 tests passed. The new control-plane CI requires 90% line / 70% branch coverage on the automation module, validates executed Cobertura, and uploads it.
- Gateway guard: five tests plus four subtests passed, including redirect, transport, timeout and credential-isolation cases. CI keeps mandatory nonempty coverage and upload gates.
- The Toto CPU diagnostic is a real native-model execution path when manually run with the existing pinned SDK. Until it completes, no successful model validation is claimed. It neither substitutes another model nor publishes a reduced-model forecast.
- Quantura's Vercel account remains blocked; no paid upgrade, DNS change, IAM change or new cloud instance is authorized by these changes. Existing application checks also report Actions artifact quota exhaustion. Those limitations are not suppressed or counted as passing tests.

References: [GitHub Contents updates and conflicts](https://docs.github.com/en/rest/repos/contents#create-or-update-file-contents), [GitHub workflow dispatch](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event), [Datadog Toto](https://github.com/DataDog/toto). The Toto 2.0.0 wheel and the registry's immutable 4M checkpoint configuration were inspected; no upstream implementation source or weights are committed here.

### Workflow parser follow-up
The new Toto diagnostic initially failed GitHub validation before any model executed: `runner.temp` is unavailable in job-level `env`. Its cache directory is now defined in step-level `env`. Gateway preflight CI runs actionlint1.7.12 against every worker workflow, with its Linux release archive verified against the pinned upstream SHA256. All workflow changes trigger this gate. This verifies context availability as well as YAML syntax; it is not evidence of a successful SDK diagnostic.

### Toto checkpoint-loading correction
The real CPU diagnostic run42143193261 failed at `native_checkpoint_load` with a `TypeError` inside the pinned Hugging Face Hub mixin before inference. The inspected toto-2 2.0.0 wheel delegates remote construction to that mixin, whereas its local loader explicitly builds `Toto2ModelConfig`. The adapter now downloads only config/safetensors for the requested checkpoint and immutable revision, then uses the SDK's local loader with `local_files_only=True`. A missing config or weights fails before SDK construction; it cannot fall back to another model. No upstream source, weights or credentials are committed.

Eight offline loader control cases pass (not model execution), with100% executed line/branch coverage for the loader. The current checkpoint and saved revision tests plus export-boundary test pass. The real native forecast and both512/511-row adapter paths must pass in the rerun before claiming recovery. The frozen historical replay runtime remains unchanged.

The next diagnostic42143833591 received HTTP401 during the authenticated snapshot download. Anonymous metadata for the same public4M revision was available. The inspected public4M checkpoint now explicitly uses `token=False`, avoiding implicit or stale account credentials; custom/private checkpoints keep their supplied token and are never retried anonymously. Nine offline loader cases pass, including denied private access without fallback. The lightweight CI job explicitly installs its required pandas/numpy versions. Real inference remains pending the next diagnostic.
