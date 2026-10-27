# Executed software validation — 2026-10-27

Python 3.12.8, pytest 9.1.1, pytest-cov 7.1.0 and coverage.py 7.17.0.

The research tests and existing paper/snapshot/hourly-comparison/limit regression
suites passed: **154 tests and 53 subtests**. The source-only research suite is
also independently runnable on the public worker without exchange/model SDKs.

Measured research-package line coverage: **805 / 823 = 97.81%**.
Measured branch coverage: **287 / 308 = 93.18%**.
These percentages apply to `market_research/sports_research`, not the whole
Quantura repository or exchange services. They are executed Python control/math
coverage, not trading-fill verification.

No first-party research files are excluded. Input validation, temporal purging,
complementary prices, fractional quantities, fees, statistics, bootstrap,
prospective registration, archive provenance and recovery are covered. Provider
and GitHub network responses use mocks in deterministic control-path tests.
The actual historical replay uses saved model outputs and public provider data
separately; it is not counted as proof of live execution.

The daily public workflow emits and validates both `coverage.xml` (Cobertura)
and `lcov.info`, uploads them as `quantura-sports-research-coverage` and fails below
90% line or 70% branch coverage. CI also rejects absent/empty reports and missing
first-party files/hit counts. Raw reports are retained for seven days. The local
research evidence folder preserves the measured reports for this change.

Reproduction commands and methodological references are in
[`../SPORTS_STRATEGY_RESEARCH.md`](../SPORTS_STRATEGY_RESEARCH.md).

An additional independent numerical check compared exact sign-test tails with
SciPy `binomtest` for four sample sizes and sample variance with NumPy. All five
checks passed; neither library is a runtime dependency of this package.

No GPU, paid compute, brokerage order or physical-hardware test was run.
