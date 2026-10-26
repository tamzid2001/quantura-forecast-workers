from copy import deepcopy
from datetime import datetime, timedelta, timezone
import math
import sys
from types import SimpleNamespace

import pytest

from metaculus_bot.tabular import TabularUnavailable, estimator, forecast, samples


def dataset(start, count):
    base = datetime(2020, 1, 1, tzinfo=timezone.utc)
    return [{"features": [float(i), 1.0], "target": 2.0 * i + 3,
             "feature_cutoff": (base + timedelta(days=2 * i)).isoformat(),
             "label_available_at": (base + timedelta(days=2 * i + 1)).isoformat()}
            for i in range(start, start + count)]


def run(**changes):
    args = {"forecast_at": "2025-01-01T00:00:00Z", "unit": "USD", **changes}
    return forecast(dataset(0, 50), dataset(51, 100), [152.0, 1.0], **args)


def test_ridge_forecast_runs_real_cpu_math_and_heldout_residuals():
    result = run()
    assert result["point"] == pytest.approx(307, abs=6)
    points = [p["value"] for p in result["percentiles"]]
    assert points == sorted(points) and points[3] == pytest.approx(307, abs=3)
    assert result["calibration_rmse"] >= result["calibration_mae"] >= 0
    assert not result["gpu_executed"] and not result["live_routing"]
    assert result["empirical_tail_resolution"] == pytest.approx(1 / 101)


def test_forecast_is_deterministic_without_mutating_the_data():
    rows = dataset(0, 50); original = deepcopy(rows)
    a = forecast(rows, dataset(51, 100), [152, 1], forecast_at="2025-01-01T00:00:00Z", unit="USD")
    b = run()
    assert a == b and rows == original


def test_training_labels_must_be_known_before_first_calibration_feature():
    training = dataset(0, 50); training[0]["label_available_at"] = "2021-01-01T00:00:00Z"
    with pytest.raises(ValueError, match="LABEL_LEAKAGE"):
        forecast(training, dataset(51, 100), [152, 1], forecast_at="2025-01-01T00:00:00Z", unit="USD")


def test_calibration_labels_must_be_available_before_the_prediction():
    with pytest.raises(ValueError, match="LABEL_LEAKAGE"):
        run(forecast_at="2020-05-01T00:00:00Z")


@pytest.mark.parametrize("edit", [
    {"features": [1, math.inf]}, {"features": [True, 1]}, {"features": [1]},
    {"label_available_at": "2019-01-01T00:00:00Z"},
    {"feature_cutoff": "2020-01-01T00:00:00"},
])
def test_invalid_shapes_values_and_timestamps_are_rejected(edit):
    rows = dataset(0, 50); rows[0].update(edit)
    with pytest.raises(ValueError):
        samples(rows)


def test_insufficient_tail_calibration_or_feature_mismatch_is_rejected():
    with pytest.raises(ValueError, match="MINIMUM"):
        forecast(dataset(0, 50), dataset(51, 20), [152, 1], forecast_at="2025-01-01T00:00:00Z", unit="USD")
    with pytest.raises(ValueError, match="SCHEMA"):
        forecast(dataset(0, 50), dataset(51, 100), [152], forecast_at="2025-01-01T00:00:00Z", unit="USD")


def test_gpu_backend_does_not_silently_fallback_or_download_models(monkeypatch):
    monkeypatch.delenv("METACULUS_TABULAR_CUML_ENABLED", raising=False)
    with pytest.raises(TabularUnavailable, match="EXPLICIT_ACTIVATION"):
        estimator("cuml_random_forest")
    with pytest.raises(ValueError, match="UNKNOWN_TABULAR_BACKEND"):
        estimator("tabfm_unlicensed_default_weights")


def test_cuml_missing_device_and_driver_errors_are_explicit_unit_control_tests(monkeypatch):
    monkeypatch.setenv("METACULUS_TABULAR_CUML_ENABLED", "true")
    monkeypatch.setitem(sys.modules, "cuml", SimpleNamespace(__version__="unit-fixture-only"))
    monkeypatch.setitem(sys.modules, "cuml.ensemble", SimpleNamespace(RandomForestRegressor=lambda **kw: None))
    runtime = SimpleNamespace(getDeviceCount=lambda: 0)
    monkeypatch.setitem(sys.modules, "cupy", SimpleNamespace(cuda=SimpleNamespace(runtime=runtime)))
    with pytest.raises(TabularUnavailable):
        estimator("cuml_random_forest")
    def broken_driver():
        raise RuntimeError("unit-fixture-driver-error")
    runtime.getDeviceCount = broken_driver
    with pytest.raises(TabularUnavailable, match="CUDA_INITIALIZATION_FAILED"):
        estimator("cuml_random_forest")


def test_optional_cuml_factory_contract_with_fake_controller_is_not_gpu_validation(monkeypatch):
    from metaculus_bot.tabular import RidgeBaseline
    monkeypatch.setenv("METACULUS_TABULAR_CUML_ENABLED", "true")
    requested = []
    def fake_forest(**kwargs):
        requested.append(kwargs)
        return RidgeBaseline()
    monkeypatch.setitem(sys.modules, "cuml", SimpleNamespace(__version__="unit-fixture-only"))
    monkeypatch.setitem(sys.modules, "cuml.ensemble", SimpleNamespace(RandomForestRegressor=fake_forest))
    monkeypatch.setitem(sys.modules, "cupy", SimpleNamespace(cuda=SimpleNamespace(runtime=SimpleNamespace(getDeviceCount=lambda: 1))))
    result = run(backend="cuml_random_forest")
    assert result["cuml_version"] == "unit-fixture-only"
    assert requested == [{"n_estimators": 200, "max_depth": 16, "random_state": 0, "n_streams": 1, "output_type": "numpy"}]


def test_empty_or_unordered_rows_and_invalid_units_are_rejected():
    with pytest.raises(ValueError):
        samples([])
    with pytest.raises(ValueError):
        samples(dataset(0, 5)[::-1])
    with pytest.raises(ValueError, match="UNITS"):
        run(unit="")
