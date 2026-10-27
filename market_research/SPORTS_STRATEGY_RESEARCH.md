# Sports strategy research and frozen paper comparisons

This package measures the actual recorded sports ledgers, including fees and
provider quantity increments. It has no exchange-order client, live promotion
path, LLM calls or model downloads. Its runtime uses the Python standard library.

## Inputs and reproducibility

Place original downloaded artifacts under `inputs/paper`, `inputs/limit` and
`inputs/month`. Keep `artifact.json` receipts with their workflow run IDs,
creation times and archive hashes. The sources are deliberately separate:

| Source | Ledger | Meaning |
|---|---|---|
| paper | comparison-ledger.csv | Published-forecast next-minute ask proxies; original-side within-10c and opposite-side outside-10c portfolios |
| limit | limit-ledger.csv | Separate ask-minus-1c experiment; only filled quantity enters returns |
| month | ledger.csv | Regenerated historical forecasts; only combined_verified_games enters this analysis |

Run from the repository root:

```sh
python -m market_research.sports_research.cli \
  --inputs /path/to/inputs --output /path/to/results --draws 2000 --seed 20261027
```

`research.json` contains complete statistics, feature availability, frozen filter
fingerprints and source hashes. `REPORT.md` contains the readable comparisons.
Input/output directories must differ. Duplicate games in a source/strategy,
non-finite cashflows, crossed quotes, partial settlement evidence and future
features fail visibly. An explicitly unavailable receipt prevents reusing an old
ledger left in the same output directory.

## What the statistics mean

- Net P&L is settlement payout minus entry principal and modeled/recorded fees.
  A high percentage of winning trades can still lose money at expensive entries.
- Sample variance and standard deviation describe dollars of net P&L per game.
  The return mean/std ratio is not annualized or presented as a Sharpe ratio.
- Returns are grouped by UTC entry day. Three observed-trading-day moving-block
  percentile bootstrap intervals keep same-day trades together. At least eight
  observed days are needed; intervals are exploratory and pointwise. They are
  not guarantees under distribution shift, unobserved games or selection bias.
- An exact one-sided sign test asks whether the median of independent nonzero
  three-day profit blocks is positive. It is not a test of mean profit. Paired
  block differences compare filters with their baseline on the same days.
- Bonferroni adjustment includes all declared candidate tests and all three
  reported stages, including underpowered/missing-feature candidates. It does
  not erase the fact that these historical datasets were already inspected.
- Training labels are purged if settlement was not available by the cutoff.
  Historical selection uses October 11, 2026 00:00 UTC within the September
  20–October 19 study. This is an exploratory chronological check, not an
  untouched holdout. The training-selected candidate remains fixed in its
  later-period comparison.
- Additional 1c/2c cost stress is charged per filled contract, including in the
  spend denominator, with recorded quantities and original fees held fixed.
  It is not a new order-book execution simulation.
- Wilson win-rate intervals assume IID games and are descriptive. Entry-quote
  Brier/log scores are baselines only; a price P01 or P99 is not a probability
  that the contract settles in the money. Cashflow-weighted break-even payout
  is reported separately from the unweighted trade win rate.
- Approximate IID sample-size calculations show sensitivity to a 5c mean gain.
  Their normal/IID assumptions exclude clustering and selection; these are
  planning calculations, not achieved statistical power.

## At-entry volatility and filters

The existing paper collectors now append one-hour quote-change variance/std,
consecutive-minute pair count, feature cutoff, forecast IQR/tail width and spread.
The signal itself, position sizing, active eligibility and order rules are
unchanged. No missing minute is bridged. At least 20 adjacent pairs are needed.
Zero variance does not produce an infinite standardized signal.

Filters veto the baseline's first selected game entry. They do not invent a
replacement proposition, a second game entry, a fill or a better price. The
finite filter family covers entry caps, spread, forecast distance, fees, obvious
props and quote volatility. Missing features are distinguished from failed
filters. Historical ledgers generally lack pre-entry volatility and report those
tests as unavailable. Excluding obvious props does not certify every remaining
market as a full-game moneyline.

## Above-hourly-P99 opposite strategy

`sports_research.p99` replays **all eligible saved forecasts**, independently of
P01-selected trade ledgers. For each genuine completed live hour:

1. The original-side final-minute ask must be strictly above that hour's P99.
2. The first recorded next-minute ask must still be above that same P99.
3. Buy the opposite side at **1 minus the original bid**, from that observation.
   Subtracting the original ask would erase the spread and overstate returns.
4. Use at most $1 entry principal, round quantity down to the provider increment,
   charge fees on the purchased side and hold to official settlement.
5. Choose one lifetime position per verified actual game across venues. Earliest
   entry wins; ties use observed overshoot and stable provider/contract IDs.
   Outcomes never rank candidates.

The baseline has no final-P99-below-50c condition. That condition, bid-confirmed
crossing, opposite-price caps, spread caps and overshoot bands are distinct
exploratory vetoes. Exact equality, interpolated model endpoints, stale quotes,
missing next-minute quotes and already closed/settled entries are excluded.
Missing histories are coverage gaps, never counted as observed no-signal hours.

This module consumes native original-side bid/ask points, a catalog row and the
saved result format from `game_p01_month`. It returns auditable candidate rows:

```python
from market_research.sports_research.p99 import candidate_rows, select, timed_trades
from market_research.sports_research.study import analyze

candidates, coverage = candidate_rows(catalog_row, saved_result, original_side_quotes)
selected = select(candidates_across_all_contracts_and_hours)
report = analyze(timed_trades(selected), source_receipts)
```

Keep raw candidates and coverage counts when replaying resumable shards. Do not
normalize unknown settlement-credit times into made-up timestamps; timed analysis
refuses them. Native model provenance matters: all seven models can participate
in a forecast while extreme quantiles combine only models supplying that tail.
No missing tail is silently synthesized by this research layer.

Historical forecasts were reconstructed after the games using pregame data.
Assumed publication times, current fee metadata and minute quote closes do not
reproduce historical model latency, depth, queue position or executable fills.
The ongoing month study is partial. No absence of eligible Polymarket US data
establishes that a strategy works or fails on that venue.

## Automatic paper review

`sports-strategy-research.yml` runs daily at **06:25 UTC** on the public
`tamzid2001/quantura-forecast-workers` repository. A repository guard prevents
private-repository compute. It verifies originating repository, branch, workflow,
event and successful completion before reading input artifacts. Source age and
the paper replay horizon are reported separately from artifact upload time.

Before analysis, the workflow saves a frozen 60-day registration starting the next
full UTC day. A two-day settlement grace period follows. The same registration
is restored even after a failed run; a missing prior registration fails closed.
Daily metrics are descriptive. Manual review requires at least 100 settled games,
20 trading days, 90% at-entry feature availability, complete sources/settlements,
positive return and improvement lower bounds, and corrected sign-test gates.
Passing those gates cannot automatically activate live trading.

The registered prospective families are the two existing P01 comparison
portfolios and the separate limit portfolio. **P99 is currently historical
research, not a claimed prospective/live collector.** It must receive its own
published-forecast collection and registration before a forward test is claimed.

Artifacts: registry 90 days; diagnostic report 14 days; coverage 7 days. Checkpoints
and records contain no exchange credentials or private memory descriptors.

## Validation

```sh
python -m pytest -q market_research/tests/test_sports_research*.py \
  --cov=market_research.sports_research --cov-branch --cov-fail-under=90 \
  --cov-report=xml:coverage.xml --cov-report=lcov:lcov.info
```

On the public worker, these same files are under `tests/`. CI validates nonempty
Cobertura and LCOV reports, first-party paths, actual hits, at least 90% line and
70% branch coverage. Tests exercise actual accounting/statistical code, malformed
inputs, time leakage, side conversion, duplicate games, registry recovery and
artifact provenance. Provider calls are mocked only for control-path tests;
these tests do not claim exchange fills or live profitability.

References actually consulted: [SciPy binomial test](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.binomtest.html),
[SciPy bootstrap](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.bootstrap.html),
[NIST Bonferroni](https://www.itl.nist.gov/div898/handbook/prc/section4/prc473.htm),
[ASA p-value statement](https://www.amstat.org/asa/files/pdfs/P-ValueStatement.pdf),
[Kalshi historical candles](https://docs.kalshi.com/api-reference/historical/get-historical-market-candlesticks),
[Polymarket US price history](https://docs.polymarket.us/api-reference/price-history/get-price-history).
