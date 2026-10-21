"""Run the existing production Quantura adapters, outside the bot's SDK venv."""
from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path


def configuration(job: dict) -> dict:
    """Use the same pinned seven-model profile as the production screeners."""
    from scripts.weekly_screener import weekly_configuration
    from .time_series import QUANTILES

    profile = weekly_configuration()
    request = {"prediction_length": job["horizon"], "frequency": job["frequency"],
               "horizon_mode": "frequency_periods", "calendar": job["calendar"], "context_length": 500,
               "transform": "none", "quantiles": QUANTILES, "failure_policy": "fail",
               "weight_mode": "auto", "models": profile["models"]}
    return {"request": request, "input": {"rows": job["rows"], "frequency": job["frequency"]},
            "model_checkpoints": profile["model_checkpoints"], "model_revisions": profile["model_revisions"],
            "weight_policy": profile["weight_policy"], "runtime_mode": "production"}


def validated_result(result: dict, job: dict, expected: set[str]) -> dict:
    """Never label a partial or equal-weight fallback as an optimized seven-model forecast."""
    from .time_series import QUANTILES

    completed = [m["model"] for m in result["model_runs"] if m["status"] == "completed"]
    if len(completed) != len(expected) or set(completed) != expected or result.get("failures"):
        raise RuntimeError("ALL_SEVEN_ENSEMBLE_MODELS_REQUIRED")
    weighting = result.get("weighting") or {}
    if (weighting.get("mode") != "auto" or weighting.get("policy") != "rolling_loss_selection_v2"
            or weighting.get("status") != "optimized"):
        raise RuntimeError("ENSEMBLE_WEIGHT_OPTIMIZATION_REQUIRED")
    matching = [r for r in result["predictions"] if r["timestamp"][:10] == job["target_date"]]
    if len(matching) != 1:
        raise ValueError("EXACT_RESOLUTION_TARGET_NOT_FORECASTED")
    values = [float(matching[0]["quantiles"][str(q)]) for q in QUANTILES]
    if not all(math.isfinite(v) for v in values) or values != sorted(values):
        raise ValueError("VALID_MONOTONE_ENSEMBLE_QUANTILES_REQUIRED")
    return {"quantiles": matching[0]["quantiles"], "models": completed,
            "weighting": {key: weighting.get(key) for key in
                          ("mode", "policy", "status", "selected_method", "calibration_rows", "weights_by_quantile")},
            "provenance": {m["model"]: {key: m.get(key) for key in
                           ("checkpoint", "checkpoint_revision", "quantile_provenance")}
                           for m in result["model_runs"] if m["status"] == "completed"}}


def run(job: dict, *, mock=False) -> dict:
    import numpy as np
    from ensemble_forecasting.worker import execute_job

    configured = configuration(job)
    seed = int(hashlib.sha256(json.dumps(job["rows"], sort_keys=True).encode()).hexdigest()[:8], 16)
    np.random.seed(seed)
    result = execute_job(configured, mock=mock)
    data = validated_result(result, job, set(configured["request"]["models"]))
    print(json.dumps({"event": "metaculus_ensemble_complete", "models": data["models"],
                      "observed_rows": len(job["rows"]), "target_date": job["target_date"],
                      "quantile_count": len(data["quantiles"]), "weight_policy": data["weighting"]["policy"],
                      "weight_status": data["weighting"]["status"],
                      "selected_method": data["weighting"]["selected_method"]}), flush=True)
    return data


if __name__ == "__main__":
    try:
        Path(sys.argv[2]).write_text(json.dumps(run(json.loads(Path(sys.argv[1]).read_text()))))
    except Exception as exc:
        # No full model traceback/request payload, headers, or credentials in logs.
        print(json.dumps({"event": "metaculus_ensemble_failed", "error_type": type(exc).__name__}))
        raise SystemExit(1)
