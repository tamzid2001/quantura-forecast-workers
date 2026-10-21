from __future__ import annotations

import copy
import io
import json
import zipfile
from types import SimpleNamespace

import pytest

from metaculus_bot.ensemble_job import configuration, validated_result

MODELS = {"prophet", "toto", "granite", "chronos", "timesfm", "timestral", "t0"}
QUANTILES = [0.01, 0.1, 0.25, 0.5, 0.75, 0.9, 0.99]


def test_production_profile_pins_all_seven_and_optimizes_only_on_history():
    rows = [{"timestamp": "2026-10-20T00:00:00Z", "target": -3.5}]
    job = {"rows": rows, "horizon": 7, "frequency": "1D", "calendar": "NONE"}
    actual = configuration(job)
    assert actual["input"]["rows"] is rows
    assert set(actual["request"]["models"]) == MODELS
    assert all(model["enabled"] for model in actual["request"]["models"].values())
    assert actual["request"]["weight_mode"] == "auto"
    assert actual["request"]["failure_policy"] == "fail"
    assert actual["request"]["context_length"] == 500
    assert actual["request"]["transform"] == "none"  # Net approval can be negative.
    assert actual["weight_policy"] == "rolling_loss_selection_v2"
    assert actual["model_checkpoints"]["toto"] == "Datadog/Toto-2.0-4m"
    assert all(len(revision) == 40 for revision in actual["model_revisions"].values())


@pytest.fixture
def completed():
    return {"model_runs": [{"model": name, "status": "completed", "checkpoint": name,
                            "checkpoint_revision": "a" * 40, "quantile_provenance": {"kind": "native"}}
                           for name in sorted(MODELS)],
            "failures": [], "predictions": [{"timestamp": "2026-10-25T00:00:00Z",
                                              "quantiles": {str(q): q * 100 for q in QUANTILES}}],
            "weighting": {"mode": "auto", "policy": "rolling_loss_selection_v2", "status": "optimized",
                          "selected_method": "equal", "calibration_rows": 28}}


def test_preserves_actual_model_and_weight_provenance(completed):
    result = validated_result(completed, {"target_date": "2026-10-25"}, MODELS)
    assert set(result["models"]) == MODELS
    assert set(result["provenance"]) == MODELS
    assert result["weighting"]["selected_method"] == "equal"  # A valid historical selection can select equal weights.
    assert result["provenance"]["toto"]["checkpoint_revision"] == "a" * 40


@pytest.mark.parametrize("mutation,error", [
    (lambda result: result["model_runs"].pop(), "ALL_SEVEN_ENSEMBLE_MODELS_REQUIRED"),
    (lambda result: result["model_runs"].append(copy.deepcopy(result["model_runs"][0])), "ALL_SEVEN_ENSEMBLE_MODELS_REQUIRED"),
    (lambda result: result["failures"].append({"model": "toto"}), "ALL_SEVEN_ENSEMBLE_MODELS_REQUIRED"),
    (lambda result: result["weighting"].update(status="fallback"), "ENSEMBLE_WEIGHT_OPTIMIZATION_REQUIRED"),
    (lambda result: result["weighting"].update(policy="rolling_pinball_v1"), "ENSEMBLE_WEIGHT_OPTIMIZATION_REQUIRED"),
    (lambda result: result["predictions"][0].update(timestamp="2026-10-26T00:00:00Z"), "EXACT_RESOLUTION_TARGET_NOT_FORECASTED"),
    (lambda result: result["predictions"][0]["quantiles"].update({"0.5": float("nan")}), "VALID_MONOTONE_ENSEMBLE_QUANTILES_REQUIRED"),
])
def test_incomplete_or_mislabeled_ensemble_cannot_be_submitted(completed, mutation, error):
    mutation(completed)
    with pytest.raises((RuntimeError, ValueError), match=error):
        validated_result(completed, {"target_date": "2026-10-25"}, MODELS)


def test_checkpoint_search_is_scoped_to_worker_and_bot(monkeypatch, tmp_path):
    from metaculus_bot import checkpoint
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_REF_NAME", "main")
    monkeypatch.setenv("METACULUS_CHECKPOINT_WORKFLOW", "metaculus-bot.yml")
    monkeypatch.setenv("METACULUS_CHECKPOINT_NAMESPACE", "bot-310211")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("metaculus-checkpoint.enc", b"encrypted-original-answer")
    urls = []

    def request(args, **kwargs):
        url = args[-1]
        urls.append(url)
        if "/workflows/" in url:
            value = {"workflow_runs": [{"id": 3, "event": "pull_request"},
                                        {"id": 2, "event": "schedule"}, {"id": 1, "event": "workflow_dispatch"}]}
        elif "/runs/2/" in url:
            value = {"artifacts": [{"id": 100, "name": "metaculus-checkpoint-bot-310227-2-discover", "expired": False}]}
        elif "/runs/1/" in url:
            value = {"artifacts": [{"id": 99, "name": "metaculus-checkpoint-bot-310211-1-discover", "expired": False}]}
        elif url.endswith("/99/zip"):
            return SimpleNamespace(stdout=archive.getvalue())
        else:
            pytest.fail(f"Unexpected checkpoint lookup: {url}")
        return SimpleNamespace(stdout=json.dumps(value).encode())

    monkeypatch.setattr(checkpoint.subprocess, "run", request)
    path = tmp_path / "checkpoint.enc"
    checkpoint.restore(path)
    assert path.read_bytes() == b"encrypted-original-answer"
    assert path.stat().st_mode & 0o777 == 0o600
    assert "branch=main" in urls[0]
    assert not any("/runs/3/" in url for url in urls)


def test_first_worker_run_can_start_without_prior_artifacts(monkeypatch, tmp_path):
    from metaculus_bot import checkpoint
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("METACULUS_CHECKPOINT_WORKFLOW", "metaculus-bot.yml")
    monkeypatch.setenv("METACULUS_CHECKPOINT_NAMESPACE", "bot-310211")
    monkeypatch.setattr(checkpoint.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=b'{"workflow_runs": []}'))
    checkpoint.restore(tmp_path / "checkpoint.enc")
    assert not (tmp_path / "checkpoint.enc").exists()
