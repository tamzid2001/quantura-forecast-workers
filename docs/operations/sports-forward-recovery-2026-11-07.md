# Sports observation recovery and execution fidelity

## Incident and preserved evidence

The hosted data gateway returned the acknowledged Vercel maintenance error.
The sports forecast workflow consequently completed its preflight but skipped
the model/publishing matrix. No current forecast artifacts existed for any of
the four sports feeds. This was already true in the November 7 pre-cleanup
artifact inventory; the repository/storage cleanup did not delete these feeds.
The hourly paper cursor correctly stopped at November 2, 05:00 UTC. No later
forecast may replace that hour's missing publication-time evidence.

The historical 82-trade tight filter (62/20, net $6.51225) and forward five-trade
sample (5/0, net $0.91105) are preserved. A recovered live paper checkpoint still
contained all 65 legacy passive-limit outcomes (42/23, net -$1.869). None of these
results is relabeled as depth-backed or continuous live coverage.

## Direct read-only market data

`native_sports.py` implements the same economic sides and completed-hour input
semantics using official public GET endpoints. Sports publication runs with
`--transport native`, without a Vercel preflight. Kalshi game discovery requires
one unambiguous linked sports milestone; neither market expiry nor opening time
is substituted for kickoff. Polymarket US uses gameStartTime/event startTime and
actual market-side IDs. Closed, unsupported and ambiguous markets are excluded.

Hourly history contains only genuine completed observations. Missing observations
are not filled. Polymarket display quotes are bucketed into completed hours;
Kalshi NO asks use one minus YES bids. Seven models, minimum-history requirements,
model/license gates, chronological weighting and pregame publication deadlines
remain enforced. The historical campaign remains pinned to its original code.

An explicit forward-only cold catalog is allowed when retained publications are
absent. The publication reports `new_forward_only_no_historical_receipts`; this
does not restore old forecasts or create pre-outage publication evidence.

Read-only probes executed locally: Kalshi NBA discovery returned 140 contracts
and one selected market supplied 93 genuine completed hourly observations.
Polymarket discovery returned 198 contracts in the first bounded page; the
selected newly listed market had zero historical observations. Zero history is
an exclusion, not a synthetic training series. These probes do not establish a
successful seven-model forecast; verify the real publishing workflow separately.

## Explicit observation epochs

Ordinary runs still fail on missing/stale history. For a genuinely unavailable
interval, an operator may dispatch the hourly paper workflow with
`resume_forward_after_gap=true`. Recovery requires all four fresh validated
publications and refuses to skip history that can already be replayed. It:

1. Preserves the previous ledger and its SHA-256 digest.
2. Records the unobserved interval, source error, original cursor, new epoch,
   recording time and verified publication artifact IDs.
3. Starts at the next future hour, never creating trades for the outage.
4. Excludes games already underway before the recovery epoch.
5. Retains lifetime fixture deduplication and the tight filter's first-choice
   veto, with no replacement trade.
6. Separates statistics by observation epoch and labels coverage discontinuous.

The old observation gap remains in reports after the workflow becomes healthy.
There is no automatic rewind, retrospective fill, ledger reset or claim that
the missing sample has been recovered. New source archives are checked against
their GitHub digest and original workflow SHA; forecast input/generation/
publication/upload timestamps must be causally ordered.

## Independent depth-based comparison

The existing forward limit worker now also records a separate, paper-only
`p01_hour_2c_observed_first_choice_depth_v2` cohort. It activates at deployment's
next hour and receives no imported historical trades or profits. It has its own
first-choice registry, $100 simulated starting cash, $1 principal budget,
quantity/tick validation, intents, receipts and results. It cannot send orders.

Candidates must satisfy the completed-hour signal, final P01 above 50 cents,
verified identity, seven-model provenance, publication timing and new-cohort
game eligibility. The observed parent-band first choice is vetoed if its hourly
discount exceeds two cents. A rejected first choice has no replacement entry.
This REST-observed candidate universe is not claimed identical to the historical
next-minute candle universe.

The execution model records a decision book and uses only the first fresh book
requested after at least one second of decision latency. Arrival must be observed
within 15 seconds, with at most five seconds of transport/receipt age. A missing
arrival does not get filled from a later favorable sample. Marketable limits
allow at most one cent of slippage and remain strictly below P01. Displayed depth
limits quantity; residual IOC quantity is canceled, zero fills stay zero, and
partial fills are retained. Cash reserves include fees and potential rounding;
unconfirmed payouts cannot fund new trades. Official settlement is credited only
when first confirmed. Missing bid marks remain missing rather than becoming zero
or optimistic equity. The legacy passive-limit simulation is unchanged.

This remains an observed-depth simulation. REST samples cannot reproduce matching
engine latency, queue priority, hidden liquidity or actual exchange fills. No
profits or live-trading readiness are inferred from unit fixtures.

The separate `depth_liquidation_equity` report walks fresh displayed bids for
the full open quantity, respects quantity increments, and deducts modeled taker
exit fees as well as the already paid entry cost and fee. Insufficient or missing
depth produces a null current portfolio value with an explicit coverage reason;
it cannot become an optimistic best-bid valuation or an invented zero price.
Prior complete samples retain their timestamps and sampled drawdown history.
The older top-bid metric remains separately labeled for comparability. Neither
sampled metric establishes continuous intratrade drawdown, a guaranteed sale, or
matching-engine execution. Raw liquidation-book evidence is retained.
Kalshi hypothetical sales round six-decimal model fees and align positive net
sale proceeds down to a cent, following the documented non-direct-member
balance precision. Buy-cost rounding is not reused for sales. Per-order rounding
rebates and account-specific FCM charges are not assumed, so these remain
conservative modeled fees rather than exchange receipt fees.

## Verification and operation

```sh
python -m pytest -q market_research/tests tests/test_paper_snapshot_limits.py
actionlint .github/workflows/hourly-game-screener.yml \
  .github/workflows/games-p01-paper.yml \
  .github/workflows/games-p01-limit-paper.yml \
  .github/workflows/sports-paper-validation.yml
```

The local regression run passed 283 tests and 59 subtests. Dedicated recovery,
native-data and depth tests produce real Cobertura coverage; CI enforces at least
90% line coverage and 70% branch coverage for the three new modules. CPU-only
tests use clearly declared original synthetic fixtures. A real checkpoint
restore preserved the legacy ledger and initialized the new depth cohort empty.

After passing review/CI and merging:

```sh
# First restore real forward publication; refresh_published must remain false.
gh workflow run hourly-game-screener.yml -R tamzid2001/quantura-forecast-workers \
  -f max_contracts=500 -f refresh_published=false
# Only after all four fresh publication artifacts are verified:
gh workflow run games-p01-paper.yml -R tamzid2001/quantura-forecast-workers \
  -f resume_forward_after_gap=true
# A short paper-only smoke run, followed by normal scheduled continuation:
gh workflow run games-p01-limit-paper.yml -R tamzid2001/quantura-forecast-workers \
  -f duration_minutes=5
```

Inspect `comparison-stats.json` for the gap and separate epochs; inspect
`depth-shadow-stats.json` and `depth-shadow-evidence.json` for the new execution
cohort. Do not interpret a running process or a successful empty catalog as
evidence of new trades. No billing settings or exchange accounts are changed.

## Technical references

- [Kalshi candlesticks](https://docs.kalshi.com/api-reference/market/get-market-candlesticks)
- [Kalshi order-book representation and complementary asks](https://docs.kalshi.com/getting_started/orderbook_responses)
- [Kalshi fee rounding, signed revenue and account precision](https://docs.kalshi.com/getting_started/fee_rounding)
- Existing shared website normalizers in `predictionMarketData.ts` and the
  provider's actual public responses. Polymarket US documentation retrieval was
  unavailable during this session; the implemented read shapes were checked
  against the existing adapter and live public GET responses instead.
