"""Optional as-of tabular regression research, isolated from live forecasting.

Features must be supplied from a reviewed resolution-source dataset. This module
does not turn arbitrary event questions into regressions or download model weights.
"""
from __future__ import annotations

import os

import numpy as np

from .evaluation import QUANTILES, number, timestamp


class TabularUnavailable(RuntimeError):
    pass


def samples(rows):
    if not 1 <= len(rows) <= 10_000:
        raise ValueError("TABULAR_SAMPLE_LIMIT")
    widths = {len(row["features"]) for row in rows}
    if len(widths) != 1 or not 1 <= next(iter(widths)) <= 128:
        raise ValueError("CONSISTENT_BOUNDED_FEATURES_REQUIRED")
    x = np.asarray([[number(v) for v in row["features"]] for row in rows], dtype=np.float64)
    y = np.asarray([number(row["target"]) for row in rows], dtype=np.float64)
    cutoffs = [timestamp(row["feature_cutoff"]) for row in rows]
    labels = [timestamp(row["label_available_at"]) for row in rows]
    if any(b <= a for a, b in zip(cutoffs, cutoffs[1:])):
        raise ValueError("UNIQUE_CHRONOLOGICAL_FEATURE_CUTOFFS_REQUIRED")
    if any(label <= cutoff for cutoff, label in zip(cutoffs, labels)):
        raise ValueError("FUTURE_LABEL_REQUIRED")
    return x, y, cutoffs, labels


class RidgeBaseline:
    """Small deterministic CPU reference; standardization sees training only."""
    def __init__(self, alpha=1.0):
        self.alpha = number(alpha)
        if self.alpha <= 0:
            raise ValueError("POSITIVE_RIDGE_PENALTY_REQUIRED")

    def fit(self, x, y):
        self.mean = x.mean(axis=0)
        self.scale = x.std(axis=0)
        self.scale[self.scale < 1e-12] = 1
        self.intercept = float(y.mean())
        standardized = (x - self.mean) / self.scale
        gram = standardized.T @ standardized + self.alpha * np.eye(x.shape[1])
        self.coefficients = np.linalg.solve(gram, standardized.T @ (y - self.intercept))
        return self

    def predict(self, x):
        return (x - self.mean) / self.scale @ self.coefficients + self.intercept


def estimator(backend):
    if backend == "ridge":
        return RidgeBaseline(), {"backend": "numpy_ridge", "numpy_version": np.__version__, "gpu_executed": False}
    if backend != "cuml_random_forest":
        raise ValueError("UNKNOWN_TABULAR_BACKEND")
    if os.environ.get("METACULUS_TABULAR_CUML_ENABLED") != "true":
        raise TabularUnavailable("CUML_EXPLICIT_ACTIVATION_REQUIRED")
    try:
        import cupy
        import cuml
        from cuml.ensemble import RandomForestRegressor
        if cupy.cuda.runtime.getDeviceCount() < 1:
            raise TabularUnavailable("CUDA_DEVICE_REQUIRED")
        # cuML documents reproducibility restrictions with concurrent streams.
        model = RandomForestRegressor(n_estimators=200, max_depth=16, random_state=0,
                                      n_streams=1, output_type="numpy")
        return model, {"backend": backend, "cuml_version": cuml.__version__,
                       "gpu_executed": True}
    except (ImportError, OSError) as error:
        raise TabularUnavailable("COMPATIBLE_CUML_CUDA_ENVIRONMENT_REQUIRED") from error
    except RuntimeError as error:
        raise TabularUnavailable("CUDA_INITIALIZATION_FAILED") from error


def forecast(training, calibration, features, *, forecast_at, unit, backend="ridge"):
    """Fit on past labels; calibrate residuals later, before forecast issuance.

    Empirical residual quantiles assume residual stability; these are not finite
    sample conformal guarantees, nor a demonstrated improvement over seven models.
    """
    if len(training) < 40 or len(calibration) < 100:
        raise ValueError("MINIMUM_40_TRAIN_100_CALIBRATION_REQUIRED")
    if not isinstance(unit, str) or not unit.strip():
        raise ValueError("NATIVE_RESOLUTION_UNITS_REQUIRED")
    train_x, train_y, train_times, train_labels = samples(training)
    cal_x, cal_y, cal_times, cal_labels = samples(calibration)
    cutoff = timestamp(forecast_at)
    if max(train_labels) >= min(cal_times) or max(cal_labels) >= cutoff:
        raise ValueError("TABULAR_LABEL_LEAKAGE")
    if train_times[-1] >= cal_times[0] or cal_times[-1] >= cutoff:
        raise ValueError("CHRONOLOGICAL_HOLDOUT_REQUIRED")
    target = np.asarray([[number(v) for v in features]], dtype=np.float64)
    if target.shape[1] != train_x.shape[1] or cal_x.shape[1] != train_x.shape[1]:
        raise ValueError("FEATURE_SCHEMA_MISMATCH")
    model, provenance = estimator(backend)
    if backend == "cuml_random_forest":
        # Explicit float32 avoids implicit downcasts at the device boundary.
        train_x, train_y, cal_x, target = [a.astype(np.float32) for a in (train_x, train_y, cal_x, target)]
        if not all(np.isfinite(a).all() for a in (train_x, train_y, cal_x, target)):
            raise ValueError("FLOAT32_RANGE_EXCEEDED")
    model.fit(train_x, train_y)
    predicted = np.asarray(model.predict(cal_x), dtype=np.float64).reshape(-1)
    estimate = np.asarray(model.predict(target), dtype=np.float64).reshape(-1)
    if predicted.shape != cal_y.shape or estimate.shape != (1,) or not np.isfinite(predicted).all() or not np.isfinite(estimate).all():
        raise ValueError("INVALID_REGRESSOR_OUTPUT")
    residuals = cal_y - predicted
    quantiles = float(estimate[0]) + np.quantile(residuals, QUANTILES, method="linear")
    if not np.isfinite(quantiles).all():
        raise ValueError("NONFINITE_RESIDUAL_QUANTILES")
    return {"percentiles": [{"percentile": q, "value": float(value)} for q, value in zip(QUANTILES, quantiles)],
            "point": float(estimate[0]), "unit": unit, "forecast_at": forecast_at,
            "calibration_mae": float(np.mean(np.abs(residuals))),
            "calibration_rmse": float(np.sqrt(np.mean(residuals ** 2))),
            "training_rows": len(training), "calibration_rows": len(calibration),
            "empirical_tail_resolution": 1 / (len(calibration) + 1),
            "uncertainty_method": "heldout_empirical_additive_residuals",
            "assumptions": ["Reviewed as-of feature and label timestamps are accurate.",
                            "Held-out residual distribution remains informative at the target horizon.",
                            "No conformal guarantee is made under temporal dependence or distribution shift."],
            "live_routing": False, **provenance}
