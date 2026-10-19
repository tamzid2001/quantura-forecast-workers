# Quantura forecast workers

Minimal on-demand execution runtime for Quantura. The application and its data remain in a separate private repository.

Only repository maintainers can dispatch jobs. Dispatch inputs contain an opaque request ID. The worker claims its private input over an authenticated HTTPS callback, reports startup before dependency installation, runs the approved immutable model configuration, and returns results to Quantura. Customer inputs, model logs, and downloaded checkpoints are never published as artifacts or caches. Temporary inputs are removed when the job finishes.

Scheduled stock, game, and perpetual screeners publish validated public market snapshots with bounded retention and zero Firestore screener writes. A separate read-only game paper portfolio checks completed-hour P01 signals twice hourly and checkpoints its cumulative ledger in GitHub artifacts. It makes no live orders and holds no exchange credentials. Quote-proxy fills and modeled fees are research estimates. Unknown cross-venue game identities are skipped. No live trading workers are included. Standard public Ubuntu runners execute the forecasts; paid larger runners are not configured. Cancellation fences the saved request before requesting cancellation of its exact GitHub run.

Hourly metered-usage delivery calls the authenticated production billing endpoint using its own narrowly scoped credential. It logs aggregate counters only and does not publish customer data. Private-repository runners are not needed for this schedule.
Existing backend maintenance schedules also run here with a separate credential limited to their allowlisted server jobs. Credentials cannot authorize customer API calls or live exchange orders. Raw server responses are never published in logs.

Runtime updates are exported using the private application's `scripts/export_forecast_runtime.py` allowlist. `runtime-manifest.json` records source commit and file checksums.
