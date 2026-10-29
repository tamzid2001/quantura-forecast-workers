# Sports paper snapshot recovery — 2026-10-27

A verified Polymarket US screener artifact expanded to 35,268,460 JSON bytes,
above the shared 32 MiB reader ceiling. The hourly replay preserved its existing
checkpoint and failed before advancing that hour.

Full screener snapshots now have a separate 128 MiB expanded-JSON ceiling
(increased from 64 MiB on October 28 at the owner's request). The
compressed artifact, individual ZIP member, state checkpoint and provider-response
limits remain at 32 MiB. Expansion still reads at most the selected cap plus one
byte and rejects an oversized result before JSON decoding. No disk extraction is
performed. Forecast timestamps, seven-model verification, rules, deduplication,
fees, entry prices and settlement behavior are unchanged.

Run `python -m unittest discover -s tests -p test_paper_snapshot_limits.py`.
A fresh paper workflow resumes the same cumulative checkpoint and unprocessed hour.
A successful start does not establish that historical catch-up has completed.
No live broker calls or orders are enabled by this change.

This is a per-snapshot parsing bound, not total backtest storage or an allocation
of laptop/cloud disk. The month campaign uses separately compressed catalog,
history and shard checkpoints; its watchdog has its own 64 MiB ZIP and 512 MiB
expanded-catalog ceilings. They are not changed by the forecast-snapshot limit.
The active month campaign keeps its original pinned inference code and rule
fingerprint. Raising this reader cap neither restarts nor accelerates inference.
