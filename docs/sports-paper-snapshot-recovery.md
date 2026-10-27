# Sports paper snapshot recovery — 2026-10-27

A verified Polymarket US screener artifact expanded to 35,268,460 JSON bytes,
above the shared 32 MiB reader ceiling. The hourly replay preserved its existing
checkpoint and failed before advancing that hour.

Full screener snapshots now have a separate 64 MiB expanded-JSON ceiling. The
compressed artifact, individual ZIP member, state checkpoint and provider-response
limits remain at 32 MiB. Expansion still reads at most the selected cap plus one
byte and rejects an oversized result before JSON decoding. No disk extraction is
performed. Forecast timestamps, seven-model verification, rules, deduplication,
fees, entry prices and settlement behavior are unchanged.

Run `python -m unittest discover -s tests -p test_paper_snapshot_limits.py`.
A fresh paper workflow resumes the same cumulative checkpoint and unprocessed hour.
A successful start does not establish that historical catch-up has completed.
No live broker calls or orders are enabled by this change.
