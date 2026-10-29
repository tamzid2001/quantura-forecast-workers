# Automatic game P01 paper portfolios

The current ask-minus-1-cent resting-limit study is a separate
[forward paper portfolio](games-p01-limit.md). The original reports below are
delayed ask-quote replays, not resting limit-order fills.

The public `quantura-forecast-workers` repository runs `games-p01-paper.yml` at
minutes 7 and 37 each hour. GitHub scheduled starts can be delayed. It consumes
existing game forecasts and public provider data; it does not rerun models,
send orders, access broker credentials, or write Firestore or Cloud Storage.

## Hourly within/outside comparison — added October 20

### Within 2c first-choice paper tracking — added October 28

`p01_hour_within_2c_first_choice_v1` separately follows the original-side rule
with **0 < timestamp-matched hourly P01 minus completed-hour ask <= $0.02**.
The 2c boundary is included. Final forecast P01 must still exceed 50%, and the
published optimized seven-model, quote freshness, identity and fee rules remain
unchanged. Entry uses the first recorded next-minute original-side ask while it
remains below P01; the entry-minute discount is not the hourly filter.

This is a **first-choice veto** of `p01_hour_within_10c_v1`. It only accepts a
game trade that the parent selected for the first time. A rejected game cannot
be replaced by another proposition, venue or later hour. That matches the
reported retrospective filter and avoids quietly changing the strategy.

The new independent portfolio starts empty at the next full hour following its
first deployed run. The prior 49W/15L, +$3.53 replay and the 15-game filtered
paper subset are historical observations, not its opening balance or forward
track record. Checkpoint recovery preserves its activation and lifetime fixture
deduplication, and validates that every entry matches a parent receipt.

The same cumulative checkpoint and comparison artifacts now contain its trades,
W/L, fees, return on spend, streaks, realized and sampled bid drawdown, recorded
funding and platform breakdowns. It uses **up to $1 purchase principal per game
plus modeled fees**, rounded down to provider quantity increments. Nominal $1
allocation is not guaranteed to be fully spent, especially for whole contracts.

This adds a descriptive forward paper ledger only. It does not enable live
orders or amend the separately frozen 60-day research design.

The same automatic workflow now checkpoints two additional independent paper
portfolios. Both use the **original side's completed-hour ask** and the P01 at
that exact forecast timestamp:

| Portfolio | Hourly condition | Purchase |
| --- | --- | --- |
| `p01_hour_within_10c_v1` | `0 < P01 − hourly ask <= $0.10` | Original signaled side |
| `p01_hour_outside_10c_opposite_v1` | `P01 − hourly ask > $0.10` | Opposite side |

The ten-cent boundary belongs to the within group. Final original-side P01
must exceed 50%, and the optimized seven-model forecast must have been published
before the hour. These are not forecasts of the opposite side: the original
signal determines the branch. Each enters at the next recorded minute ask,
retaining the original requirement that the original-side follow-up ask is
still below P01. Opposite ask is **1 minus original bid**, including spread,
not 1 minus original ask. No passive-limit fill is assumed.

Each portfolio uses $1 principal plus modeled fees, rounds down to published
quantity increments and permits one lifetime entry per verified game across
Kalshi and Polymarket US, including other propositions. Unsupported or missing
increments skip the candidate; an absent Kalshi fractional flag conservatively
uses whole contracts. Every filled position holds to official settlement.
Each portfolio has its own exposure ledger; their returns are independent and
must not be added as a funded shared-account result.

New portfolios start at the next full hour after their first deployment run.
Older checkpoints are upgraded while retaining all original trades and rule
hashes. The earlier retrospective estimates are not imported as forward results.
Activation, rule hashes, coverage and cumulative trades survive restarts.

In `quantura-games-p01-paper-stats`, **`comparison-stats.json`** and
**`comparison-ledger.csv`** provide the direct two-strategy comparison. Reports
also appear side by side in each GitHub run summary and `comparison-summary.md`.
The JSON/CSV reports
include W/L, net win rate, gross/net P&L, fees, return on entry spend,
current/maximum W/L streaks, average entry and duration, open positions/cost,
realized drawdown, sampled bid-equity drawdown and missing-mark coverage,
recorded cash-flow funding, venue breakdowns and source-read errors. CSV rows
retain the original signal side, bought side, hour quote, P01, discount,
publication time and source artifact/prediction hash. A concise log line prints
the main statistics even when GitHub truncates a long JSON line.

Quote replay entries are simulations without historical book-depth guarantees.
Recorded funding and sampled drawdown are not a true minimum deposit or a
continuous equity-risk estimate. Schedule delays are possible; reports expose
the last run time and unprocessed hour. The separate ask-minus-one-cent
[forward limit worker](games-p01-limit.md) continues with its own execution
rules and ledger.

## Original frozen rules

The original portfolios retain approximately $1 notional plus modeled fees per game:

- `p01_one_game_v1`: final predicted P01 > 50%; selected-side ask below the
  timestamp-matched P01 at the completed hour and next recorded minute.
- `p01_shallow_10c_v1`: the same rule, with the entry ask no more than 10 cents
  below that hour's P01. A higher price within this band is not automatically a
  better trade. The band was selected retrospectively and needs prospective data.

Each independently permits one entry per actual game for its entire lifetime,
including other sides, propositions, subsequent hours and the other venue.
Different games can overlap. Simultaneous candidates rank by P01 minus ask minus
the modeled one-contract fee; no outcome or later quote enters that ranking.
Unknown or ambiguous cross-venue identities are excluded and counted in coverage.
Names must match a reviewed alias or unique exact participant pair; singles
tennis also accepts unique paired surname/full-name matches within the same tour.
Rematches beyond the three-hour identity window are excluded. This deliberately
reduces coverage rather than count the same game twice.

Only schema 3, seven-model, automatic `rolling_loss_selection_v2` snapshots
published before the signal hour qualify. Exact source artifact and prediction
hash are recorded. P01 pooling uses models with native tail support. No history
is forward-filled. Each portfolio holds to official settlement.

## Persistence and measurements

`quantura-games-p01-paper-v1` contains one cumulative `paper-state.json.gz`.
Each run validates main-branch workflow provenance, restores the newest checkpoint,
and checkpoints each completed hour. A failed public data read leaves that hour
pending. Artifact upload runs even on failure. State rules are fingerprinted;
changing rules requires a new portfolio. Snapshots older than six hours and
checkpoint gaps beyond 48 hours require review instead of silent backfilling.
The worker refuses to start an empty ledger if a previous completed run exists
but its checkpoint has disappeared. Initial runner queue delays do not backdate
the prospective activation time.

`quantura-games-p01-paper-stats` contains readable JSON and a CSV ledger. It
reports W/L, net win rate, current and maximum streaks, modeled fees, gross/net
P&L, average entry, average duration, realized drawdown, open cost, historical
cash-flow funding minimum and total entry spend without payout reuse.

Bid-equity drawdown is sampled twice hourly only when every open position has a
genuine bid no older than 120 seconds. Missing bids remain unmarked and coverage
is reported; it is not a continuous or intraminute maximum drawdown. Realized
drawdown is calculated separately in settlement order.

For prospective accounting, payouts are credited at the first confirmed paper
receipt, not an assumed end time. Duration therefore includes settlement polling
delay. The funding minimum covers only the recorded cash-flow sequence and is
not a recommended deposit, exchange funding minimum or guarantee for future trades.

Kalshi fees use the validated general M=1 quadratic schedule and conservative
whole-cent total-cash rounding. Other fee schedules are excluded. Polymarket US
fees use the market coefficient and half-even cent rounding. No rebates or
depth/slippage assumptions are credited. Quote replays are not live fills.

## Operations

Set `GAMES_P01_PAPER_ENABLED=true` and `GAMES_P01_PAPER_ACTIVATED_AT` to activation
UTC, then dispatch the public workflow once. The first eligible hour is after
activation; historical research is never relabeled as prospective paper trading.
To pause, set `GAMES_P01_PAPER_ENABLED=false`. Old live Kalshi workers are unrelated
and are not activated by this workflow.
