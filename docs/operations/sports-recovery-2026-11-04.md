# Sports research recovery — November 4, 2026

The retrospective campaign's last run (`41519705762`) omitted shard 3 after an earlier failure. The report therefore omitted those observations. Automatic continuation previously depended only on report success and propagated incomplete checkpoints. Continuation now requires successful replay (or an intentionally skipped replay during catalog enumeration).

The last retained shard 3 is from run `41152246865`. Both catalogs have the same recomputed contract hash `1946babc44c910c99e370e70262fd7e1e11307e60ddac70b482bd266c1389d2d`, date range September 20–October 19, and frozen runtime fingerprint `eb11dc7f11952554ed3206c16cb8f9ccb8db7b2b9feeb0f69497065d4cf0834f`.

A manual recovery dispatch can specify `recovery_sources={"3":41152246865}` with `resume_run_id=41519705762` and `code_sha=a1b30ef9526bc39d3b26154a87437a8d687f3e3b`. Shards 0–2 remain at the latest checkpoint; shard 3 resumes its older saved state. The controller checks run provenance, actual catalog contents, model-runtime identity, shard partition membership and bounded decompression before restoring. It emits a hash receipt. It does not change models, historical entry rules, or outcomes. Subsequent complete runs resume normally.

Forward paper replay no longer treats cursor age alone as proof of lost data. It discovers artifacts back to the cursor and replays at most 12 consecutive hours per invocation. Missing/stale snapshots still stop at the original hour. No gap is skipped; original publication-time and game deduplication checks remain. Settlement reconciliation continues even if new entries are blocked. `catchup_pending` distinguishes a successful bounded batch from full coverage.

Validation: `python -m pytest -q market_research/tests` passed 180 tests and 53 subtests locally. `actionlint` passed the modified workflows. Tests include missing checkpoints, forged catalog hashes, wrong shards, untrusted sources, compressed-input bounds, multi-day cursors, missing feed preservation and settlement reconciliation after a feed failure.

The sports screener currently depends on Quantura's Vercel data API, which returns HTTP 402 during the team allowance block. No missing forecast or quote is fabricated. A resumed backtest remains incomplete until all shards finish. Forward paper results are quote/fill proxies, not live exchange fills.
