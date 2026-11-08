GitHub no longer retains the catalog for the latest sports-month run, so the report and recovery watchdog cannot resume even though all four shard ledgers survive. Restore only the approved frozen catalog from a separately retained release backup, verifying both its file checksum and recomputed contract-content hash against the campaign. Preserve all four latest ledgers and the original seven-model runtime. The report job also uses this recovery path if the catalog disappears during a long replay.

Metaculus previously failed when an artifact disappeared after discovery. Re-discover only on HTTP 404/410, retry at most three times, refuse older or missing replacement state, verify available artifact digests, and atomically replace the encrypted checkpoint. Other HTTP failures and invalid archives remain failures. Private-note quotas and forecasting rules are unchanged.

Validation: 329 Metaculus tests passed; sports catalog/shard/month tests passed; actionlint and git diff checks passed. Downloaded all four latest November 8 ledgers and verified their GitHub SHA256 receipts, catalog membership, shard assignments and statuses. The live recovery helper downloaded and validated the retained 272,642-contract catalog. Generated a fresh local report using the original pinned runtime; the study remains incomplete. A second copy of all four ledgers and the matching catalog is retained in the existing private recovery archive.

## Recovery controls

- Campaign runtime: `a1b30ef9526bc39d3b26154a87437a8d687f3e3b`.
- Latest surviving shard run: `42508661481`. Every shard has catalog hash `1946babc44c910c99e370e70262fd7e1e11307e60ddac70b482bd266c1389d2d`.
- Independent catalog copy: [frozen-catalog recovery release](https://github.com/tamzid2001/quantura-forecast-workers/releases/tag/sports-month-frozen-catalog-20261108).
- Catalog compressed-file SHA256: `73673c4ba80ce69d72ad7c33905eccc22d7e8a4102720d77f96df8cee97d4266`.
- This recovery never reconstructs or initializes a replay shard. Missing cumulative ledgers still require review. Invalid existing catalog bytes cannot silently fall back to the release.
- The most recent MiniBench audit reports 56 of 60 forecasts with all 56 private reasoning notes verified. Four closed questions cannot be submitted retrospectively. The currently running bot is healthy; this does not establish complete historical tournament coverage.
- The hourly paper ledger from the last successful run has also disappeared. The latest retained local copy predates additional observed trades; do not replace it without accounting for the missing ledger interval. Its protective stop remains necessary until that separate recovery is verified.
