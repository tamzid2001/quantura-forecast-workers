# Quantura forecast workers

Minimal on-demand execution runtime for Quantura. The application and its data remain in a separate private repository.

Only repository maintainers can dispatch jobs. Dispatch inputs contain an opaque request ID. The worker claims its private input over an authenticated HTTPS callback, reports startup before dependency installation, runs the approved immutable model configuration, and returns results to Quantura. Customer inputs, model logs, and downloaded checkpoints are never published as artifacts or caches. Temporary inputs are removed when the job finishes.

Scheduled stock, game, and perpetual screeners publish validated public market snapshots with bounded retention and zero Firestore screener writes. There are no trading or historical research workers. Standard public Ubuntu runners execute the forecasts; paid larger runners are not configured. Cancellation fences the saved request before requesting cancellation of its exact GitHub run.

Runtime updates are exported using the private application's `scripts/export_forecast_runtime.py` allowlist. `runtime-manifest.json` records source commit and file checksums.
