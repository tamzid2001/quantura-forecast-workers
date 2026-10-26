# Metaculus research and evaluation

This research toolkit complements the live seven-model numerical route and free
reasoning agents. It does not automatically add an eighth live time-series model,
alter accepted forecasts, or promote fitted calibration parameters.

## What is deployed

The first upgraded public-runtime production run,
[40267233723](https://github.com/tamzid2001/quantura-forecast-workers/actions/runs/40267233723),
confirmed five submissions with private notes, all through NVIDIA MiniMax after
OpenRouter capacity failure. Own-history receipts found five forecasts with no
scheduled withdrawal. Those are operational results, not accuracy or prize results.
The official [bot resources](https://www.metaculus.com/notebooks/38928/bot-tournament-resources-page/)
specify one forecast per MiniBench/FutureEval question. Availability failover does
not reforecast an accepted answer, and open tournament questions are not test cases.

## Proper scoring from immutable forecast records

Run the offline evaluator on JSONL with one independently resolved record per
question. Records need `question_id`, `question_type`, `mode` (`prospective` or
`replay`), timezone-aware `forecast_at`, `evidence_available_at`, `close_time`,
`resolved_at`, `answer`, and `outcome`. Numeric/discrete rows also require native
`unit`. Dates and date quantiles use ISO timestamps. Binary outcomes are integers
0/1; multiple-choice outcomes use exact option strings. Numerical answers contain
P01/P10/P25/P50/P75/P90/P99 in original units.

```bash
python -m metaculus_bot.evaluation private-resolved-records.jsonl \
  --as-of 2026-10-26T00:00:00Z --out artifacts/evaluation.json
```

Join actual platform submission receipts to independently sourced resolved labels.
A draft generation time is not a submission receipt. The encrypted worker audit
now records question type, closing time, requested as-of time and evidence
retrieval timestamps to support that join. Retrieval time establishes when the
bot fetched a page, not when its underlying information first became public.
FRED observation dates likewise are not release timestamps. For historical tests,
use frozen evidence available at the historical cutoff; current linked pages and
revised economic data cannot establish historical availability.

The evaluator rejects duplicate questions, late evidence, forecasts at/after
close, unresolved outcomes at the scoring cutoff and mixed numeric units. It
reports binary Brier, natural-log loss and reliability/ECE bins; multiclass log
loss and the sum-over-classes Brier score; and native-unit quantile pinball,
median MAE and P10–P90 interval coverage. Prospective and replay groups stay
separate. A 1e-15 numerical floor bounds reported log loss for impossible events;
it does not change the saved prediction. These are not Metaculus peer scores:
peer distributions and tournament score weighting are not supplied. Sparse
quantile pinball is not the complete numerical CDF log score.

Chronological timestamp checks detect observable leakage; they cannot exclude
historical outcomes from an LLM's pretraining. Retrospective readiness results
must never be described as clean prospective performance.

## Calibration and base-rate discipline

`evaluation.select_temperature(training, validation, as_of=...)` fits a fixed
candidate grid by training log loss, then accepts it only if later validation
improves log loss without worsening Brier. It requires at least 100 training and
50 validation questions; all training outcomes must have been known before the
first validation forecast. Question IDs cannot overlap, and prospective/replay
provenance cannot be mixed. The returned artifact includes a data digest,
counts, scores and an explicit deployment-disabled status. An untouched final
test period and operator review are still required. Five readiness questions
are insufficient to select calibration or ensemble weights.

Question-specific reference classes must be defined before observing outcomes.
No universal sports/election/market base rate is injected into unrelated events.
The live agent separates supplied evidence from assumptions and abstains when
criteria/evidence cannot support a defensible prediction. Linked public excerpts
and bounded FRED evidence are available; AskNews, paid search, Jev paid calls and
other unconfigured search services are not silently assumed connected.

## Tabular regression candidates

### Google Research TabFM

Inspected the [official repository](https://github.com/google-research/tabfm),
[regression example](https://github.com/google-research/tabfm/blob/main/examples/regression_example.py)
and [Google Research introduction](https://research.google/blog/introducing-tabfm-a-zero-shot-foundation-model-for-tabular-data/).
Reviewed source revision: `fbb665569425fd2f490c6576b3af967876fe11ff`. The documented estimator fits a structured feature matrix and target vector;
it cannot infer a resolution variable or dataset from arbitrary question text.
Its default pretrained weights have the **TabFM non-commercial v1.0** license,
while repository code is Apache 2.0. That code license does not license the
weights for Quantura production or prize-seeking use. No default TabFM checkpoint
is downloaded, loaded or promoted here. A separately licensed checkpoint and
version/quality review would be required. No claim that it is the best regression
model for these questions has been established.

### NVIDIA RAPIDS cuML

Inspected the [official RandomForestRegressor API](https://docs.nvidia.com/cuml/latest/api/generated/cuml.ensemble.RandomForestRegressor/)
and [RAPIDS cuML source](https://github.com/rapidsai/cuml). The optional adapter uses
the public estimator, explicit float32 device inputs, a fixed seed, one stream
and NumPy outputs. `METACULUS_TABULAR_CUML_ENABLED=true` is required along with a
compatible installed cuML/CuPy/CUDA stack and a real CUDA device. Missing hardware,
SDKs or drivers fails explicitly; it is never described as GPU execution after
CPU fallback. RAPIDS is not installed in the lightweight bot SDK environment.
**GPU fitting and numerical parity: NOT RUN — GPU/RAPIDS UNAVAILABLE locally.**

### Executable CPU baseline and held-out residuals

`tabular.forecast(training, calibration, features, forecast_at=..., unit=...)`
provides a real deterministic NumPy ridge baseline and the optional cuML backend.
Each training/calibration row has `features`, `target`, `feature_cutoff` and
`label_available_at`. Labels must become available after their feature cutoff;
all training labels precede calibration, and calibration labels precede the target
forecast. Validation enforces finite values, bounded dimensions, consistent
feature schemas, chronological unique cutoffs and native units. Standardization
uses training data only. There must be at least 40 training and 100 later
calibration observations.

The output combines a point estimate with empirical held-out additive residual
quantiles. It records calibration MAE/RMSE, native units, sample counts, tail
resolution and backend provenance. Residual stability is an explicit assumption;
these are not conformal guarantees under temporal dependence or structural
change. Neither this baseline nor optional cuML is routed into live questions
without a reviewed data mapping and an untouched out-of-sample comparison.
Synthetic linear-data tests validate implementation, not economic performance.

## Validation and coverage

```bash
python -m pip install --require-hashes -r metaculus_bot/requirements.lock
python -m pytest -q metaculus_bot/tests --cov \
  --cov-config=metaculus_bot/coverage.ini \
  --cov-report=term-missing --cov-report=xml:artifacts/metaculus-coverage.xml
```

Local result: **221 tests passed** on Python 3.14.2/macOS. Scoped new-module
coverage: **273/283 lines (96.47%)**, **119/128 branches (92.97%)** for probability,
evaluation and tabular modules. The XML contains actual executed line hits.
CI runs the same full bot tests on Python 3.12, validates the report, requires
90% line coverage per new module plus 70% overall scoped branch coverage and
persists the raw Cobertura artifact for 30 days. This is not whole-application
coverage, CUDA kernel coverage or physical GPU validation. cuML control-path
fixtures only test software contracts and rejection paths.
