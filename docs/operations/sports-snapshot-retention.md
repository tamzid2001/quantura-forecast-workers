# Point-in-time game forecast retention

Hourly game snapshots are retained for seven days instead of three. The November 1 Vercel outage exposed that a previously processed checkpoint can be less than three days old while its last eligible snapshot is older and has already expired. Increasing retention protects future recovery windows; it does not recreate missing historical observations.

The paper tracker still stops on genuinely missing or stale feeds and preserves its cursor. Every retained snapshot must predate the signal and pass forecast/source identity checks. Empty, recomputed or current snapshots cannot substitute for a missing historical publication. No trade is inserted for an uncovered hour.

The monthly backtest uses separate 90-day compact checkpoints and a pinned seven-model runtime. Its recovery receipts record the exact frozen catalog and source run for each shard. Research history caches are reproducible API downloads and remain short-lived.
