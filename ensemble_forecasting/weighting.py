"""Bounded, chronological ensemble calibration with a separate audit window.

Weights minimize regularized pinball loss per quantile in target units. This is
an optimum for the calibration objective, not a promise about future accuracy.
The last window is scored only after the weights have been frozen.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

import numpy as np
from scipy.optimize import linprog

from .capabilities import MODEL_REGISTRY
from .ensemble import effective_weights_by_quantile
from .evaluation import HISTORICAL_VALIDATION_POLICY, outcome_key, score_predictions
from .schemas import ForecastRequest, PreparedSeries, canonical_quantile_string

WEIGHT_POLICY = "rolling_pinball_v1"
LOSS_SELECTION_POLICY = "rolling_loss_selection_v2"
MIN_CALIBRATION_ROWS = 10
REGULARIZATION = 0.02


def fit_quantile_weights(predictions: np.ndarray, actuals: np.ndarray, q: float,
                         prior: np.ndarray) -> np.ndarray:
    """Solve a convex pinball-loss problem; bounded weights discourage collapse."""
    n, m = predictions.shape
    if n < MIN_CALIBRATION_ROWS or m < 1 or actuals.shape != (n,) or prior.shape != (m,):
        raise ValueError("insufficient calibration data")
    if not np.isfinite(predictions).all() or not np.isfinite(actuals).all() or not np.isfinite(prior).all():
        raise ValueError("nonfinite calibration data")
    if m == 1:
        return np.ones(1)
    prior = prior / prior.sum()
    # Normalize residuals, not price levels, so pip-sized series remain stable.
    scale = max(float(np.median(np.abs(predictions - actuals[:, None]))), 1e-9)
    x = (predictions - actuals[:, None]) / scale
    # Variables: model weights, positive/negative residuals, L1 prior distances.
    objective = np.r_[np.zeros(m), np.full(n, q / n), np.full(n, (1 - q) / n),
                      np.full(m, REGULARIZATION)]
    equality = np.zeros((n + 1, 2 * m + 2 * n))
    equality[:n, :m] = x
    equality[:n, m:m + n] = np.eye(n)
    equality[:n, m + n:m + 2 * n] = -np.eye(n)
    equality[n, :m] = 1
    inequalities = np.zeros((2 * m, 2 * m + 2 * n))
    inequalities[:m, :m] = np.eye(m)
    inequalities[:m, m + 2 * n:] = -np.eye(m)
    inequalities[m:, :m] = -np.eye(m)
    inequalities[m:, m + 2 * n:] = -np.eye(m)
    solution = linprog(objective, A_ub=inequalities, b_ub=np.r_[prior, -prior],
                       A_eq=equality, b_eq=np.r_[np.zeros(n), 1.],
                       bounds=[(0, .8)] * m + [(0, None)] * (2 * n + m), method="highs")
    if not solution.success or not np.isfinite(solution.x[:m]).all():
        raise ValueError("weight optimizer failed")
    weights = np.maximum(solution.x[:m], 0)
    return weights / weights.sum()


def _pool(components: Mapping[str, list[dict]], weights: Mapping[str, Mapping[str, float]],
          quantiles: tuple[float, ...]) -> list[dict]:
    first = next(iter(components.values()))
    matrix = np.array([[sum(weight * components[name][i]["quantiles"][key] for name, weight in weights[key].items())
                        for i in range(len(first))] for key in map(canonical_quantile_string, quantiles)])
    matrix.sort(axis=0)  # Same deterministic crossing repair as the future ensemble.
    return [{"timestamp": row["timestamp"], "quantiles": {canonical_quantile_string(q): float(matrix[j, i])
             for j, q in enumerate(quantiles)}} for i, row in enumerate(first)]


def optimize_weights(job: Mapping[str, Any], series: PreparedSeries, request: ForecastRequest,
                     *, execute: Callable, progress: Callable, mock: bool) -> tuple[dict, dict | None]:
    prior = effective_weights_by_quantile(request)
    policy = job.get("weight_policy") or WEIGHT_POLICY
    if policy not in {WEIGHT_POLICY, LOSS_SELECTION_POLICY}:
        raise ValueError("unsupported weight policy")
    calibration_folds = 4 if policy == LOSS_SELECTION_POLICY else 2
    total_folds = calibration_folds + 1
    enabled = [name for name, selection in request.models.items() if selection.enabled and selection.weight > 0]
    report: dict[str, Any] = {"mode": "auto", "policy": policy, "status": "fallback",
                              "reason": "insufficient_history", "pooling_space": "transformed",
                              "weights_by_quantile": prior, "calibration_rows": 0,
                              "regularization": REGULARIZATION, "max_model_weight": .8,
                              "folds": []}
    if policy == LOSS_SELECTION_POLICY:
        report.update(regularization=None, max_model_weight=1., calibration_folds=4,
                      inner_validation_folds=2, candidate_count=25, prior_policy="equal_eligible_models")
    if len(enabled) == 1:
        report.update(status="single_model", reason="one_enabled_model", max_model_weight=1.)
        return report, None
    minimum = max(2, *(int(MODEL_REGISTRY["models"][name].get("minimumObservedContext", 2)) for name in enabled))
    horizon = min(30, max(5, request.prediction_length), (len(series.values) - minimum) // total_folds)
    report["calibration_horizon"] = max(0, horizon)
    if horizon < 5:  # Two calibration windows must supply at least ten real matches.
        return report, None
    key = outcome_key(job, series, request)
    calibration: list[tuple[dict, float]] = []
    groups: list[list[tuple[dict, float]]] = []
    learned = prior
    try:
        for fold in range(total_folds):
            split = len(series.values) - (total_folds - fold) * horizon
            training = [{"timestamp": stamp, "target": float(value)}
                        for stamp, value in zip(series.timestamps[:split], series.values[:split])]
            actuals = dict(zip(series.timestamps[split:split + horizon], map(float, series.values[split:split + horizon])))
            fold_request = {**job["request"], "weight_mode": "manual", "prediction_length": horizon, "failure_policy": "fail"}
            if request.horizon_mode == "calendar_days":
                fold_request["horizon_mode"] = "trading_sessions"
            fold_job = {**job, "request": fold_request, "input": {**dict(job["input"]), "rows": training,
                        "timestamp_column": "timestamp", "target_column": "target"}}
            fold_job.pop("evaluation_policy", None)
            fold_job.pop("dataset_hash", None)
            result = execute(fold_job, mock=mock, minimum_history_rows=minimum, include_components=True,
                             progress=lambda payload: progress({**payload, "phase": "weight_optimization"}))
            components = result.pop("_components")
            metadata = {"role": "calibration" if fold < calibration_folds else "audit", "training_rows": split,
                        "training_end_at": series.timestamps[split - 1],
                        "validation_start_at": series.timestamps[split],
                        "validation_end_at": series.timestamps[split + horizon - 1],
                        "holdout_rows": horizon, "prepared_series_hash": result["prepared_series_hash"]}
            observed = {key(stamp, True): value for stamp, value in actuals.items()}
            matched = [(i, observed[key(row["timestamp"], False)])
                       for i, row in enumerate(result["predictions"]) if key(row["timestamp"], False) in observed]
            metadata["matched_rows"] = len(matched)
            report["folds"].append(metadata)
            if fold < calibration_folds:
                group = [({name: rows[i]["quantiles"] for name, rows in components.items()}, value)
                         for i, value in matched]
                groups.append(group)
                calibration.extend(group)
                if fold == calibration_folds - 1:
                    learned = {}
                    if policy == LOSS_SELECTION_POLICY:
                        from .loss_weights import choose_on_calibration, weights
                        if any(len(group) != horizon for group in groups):
                            raise ValueError("incomplete calibration origins")
                        predictions = np.full((len(enabled), calibration_folds, len(request.quantiles), horizon), np.nan)
                        outcomes = np.array([[value for _, value in group] for group in groups])
                        for mi, name in enumerate(enabled):
                            for qi, q in enumerate(request.quantiles):
                                qkey = canonical_quantile_string(q)
                                if name in prior[qkey]:
                                    predictions[mi, :, qi, :] = [[pred[name][qkey] for pred, _ in group] for group in groups]
                        # A shared candidate needs a common trained probability.
                        # Tail-only/custom requests retain the validated LP path.
                        common = np.isfinite(predictions).all(axis=(1, 3)).all(axis=0)
                        if common.any():
                            method, scores = choose_on_calibration(predictions, outcomes, request.quantiles)
                            fitted = weights(predictions, outcomes, request.quantiles, method)
                            for qi, q in enumerate(request.quantiles):
                                qkey = canonical_quantile_string(q)
                                learned[qkey] = {name: float(fitted[qi, mi]) for mi, name in enumerate(enabled) if name in prior[qkey]}
                            report.update(selected_method=method, inner_scores=scores)
                    if not learned:
                        for q in request.quantiles:
                            qkey = canonical_quantile_string(q)
                            names = list(prior[qkey])
                            x = np.array([[pred[name][qkey] for name in names] for pred, _ in calibration])
                            y = np.array([value for _, value in calibration])
                            w = fit_quantile_weights(x, y, q, np.array([prior[qkey][name] for name in names]))
                            learned[qkey] = dict(zip(names, map(float, w)))
                        if policy == LOSS_SELECTION_POLICY:
                            report.update(selected_method="per_quantile_lp_no_common_support", max_model_weight=.8)
                    report.update(calibration_rows=len(calibration))
            else:
                predictions = _pool(components, learned, request.quantiles)
                scores = score_predictions(predictions, actuals, request.quantiles, key)
                baseline = score_predictions(_pool(components, prior, request.quantiles), actuals, request.quantiles, key)
                audit = {"policy": HISTORICAL_VALIDATION_POLICY, "method": "chronological_holdout",
                         "status": "completed" if scores["count"] else "no_matching_outcomes",
                         "minimum_training_rows": minimum, "requested_models": enabled, "metrics": scores,
                         **{k: v for k, v in metadata.items() if k not in {"role", "matched_rows"}},
                         "frequency": series.frequency, "evaluated_at": datetime.now(timezone.utc).isoformat(),
                         "actuals_hash": hashlib.sha256(json.dumps(actuals, sort_keys=True).encode()).hexdigest(),
                         "prediction_length": result["prediction_length"], "transform": result["transform"],
                         "effective_weights_by_quantile": learned,
                         "evidence": {"predictions": predictions, "actuals": actuals}}
                report.update(status="optimized", reason=None, pooling_space="target", weights_by_quantile=learned,
                              audit_metrics=scores, baseline_audit_metrics=baseline)
                json.dumps(report, allow_nan=False)
                return report, audit
    except Exception:
        # Keep the successful future forecast. Failures never silently switch to
        # in-sample scores or optimize on the audit outcomes.
        report.update(reason="calibration_failed")
        logging.getLogger("quantura.ensemble.worker").warning("weight calibration failed: code=WEIGHT_CALIBRATION_FAILED")
    return report, None
