"""Offline loader control tests; real model execution is a separate diagnostic."""
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from ensemble_forecasting.adapters.toto_loader import load_toto_model
from ensemble_forecasting.adapters.checkpoint_access import checkpoint_location, checkpoint_token


@pytest.mark.parametrize("checkpoint,expected", [
    ("Datadog/Toto-2.0-4m", False),
    ("ibm-granite/granite-timeseries-patchtst-fm-r2", False),
    ("amazon/chronos-2", False),
    ("google/timesfm-3.0-pytorch", False),
    ("mistralai/Timestral-209M", False),
    ("theforecastingcompany/t0-gamma", False),
    ("private-org/model", "fixture-credential"),
    ("private-org/Toto-2.0-4m", "fixture-credential"),
    ("ibm-granite/granite-timeseries-patchtst-fm-r2-private", "fixture-credential"),
])
def test_only_exact_inspected_public_ids_disable_account_authentication(checkpoint, expected):
    assert checkpoint_token(checkpoint, "fixture-credential") == expected


def test_private_checkpoint_without_a_credential_does_not_gain_one():
    assert checkpoint_token("private-org/model", None) is None


@pytest.mark.parametrize("access,commercial,code", [
    ("false", "true", "LICENSE_ACCESS_NOT_APPROVED"),
    ("true", "false", "LICENSE_COMMERCIAL_LICENSE_REQUIRED"),
])
def test_public_download_policy_does_not_bypass_timesfm_production_gates(monkeypatch, access, commercial, code):
    from ensemble_forecasting.adapters.timesfm import TimesFMAdapter
    from ensemble_forecasting.adapters.base import ModelExecutionError
    from ensemble_forecasting.schemas import ForecastRequest

    monkeypatch.setenv("TIMESFM_HF_ACCESS_APPROVED", access)
    monkeypatch.setenv("TIMESFM_COMMERCIAL_LICENSED", commercial)
    monkeypatch.setenv("ALLOW_NONCOMMERCIAL_TIMESFM", "true")
    download = Mock(side_effect=AssertionError("No download permitted"))
    monkeypatch.setattr("ensemble_forecasting.adapters.timesfm.checkpoint_location", download)
    request = ForecastRequest.from_dict({"prediction_length": 7, "horizon_mode": "frequency_periods", "quantiles": [.1,.5,.9],
        "models": {"timesfm": {"enabled": True, "weight": 1}}, "runtime_mode": "production"})
    with pytest.raises(ModelExecutionError) as error:
        TimesFMAdapter().forecast(None, (), request)
    assert error.value.code == code
    download.assert_not_called()


@pytest.mark.parametrize("checkpoint", ["Datadog/Toto-2.0-4m", "ibm-granite/granite-timeseries-patchtst-fm-r2", "amazon/chronos-2", "private-org/model"])
def test_local_snapshot_resolution_is_explicit_and_private_models_remain_remote(checkpoint):
    with patch('ensemble_forecasting.adapters.checkpoint_access.download_safetensors_snapshot',return_value='/fixture/cache') as download:
        result=checkpoint_location(checkpoint,'a'*40,token='fixture-credential')
    if checkpoint=='private-org/model':
        assert result==checkpoint
        download.assert_not_called()
    else:
        assert result=='/fixture/cache'
        download.assert_called_once_with(checkpoint,'a'*40,token='fixture-credential')


@pytest.mark.parametrize("sharded", [False, True])
@pytest.mark.parametrize("revision", [None, "8306a9801cf98c0f5ffe4b2dcc8f496e616d84d9"])
def test_loader_preserves_checkpoint_revision_and_isolates_local_sdk_load(tmp_path, sharded, revision):
    (tmp_path / "config.json").write_text("{}")
    weights = "model.safetensors.index.json" if sharded else "model.safetensors"
    (tmp_path / weights).write_bytes(b"fixture-not-model-weights")
    download = Mock(return_value=str(tmp_path))
    sdk = Mock()
    with patch.dict("sys.modules", {
        "huggingface_hub": SimpleNamespace(snapshot_download=download),
        "toto2": SimpleNamespace(Toto2Model=sdk),
    }):
        result = load_toto_model("Datadog/Toto-2.0-4m", revision, token="fixture-credential")
    download.assert_called_once_with(
        repo_id="Datadog/Toto-2.0-4m", revision=revision, token=False,
        allow_patterns=["config.json", "*.safetensors", "*.safetensors.index.json"],
    )
    sdk.from_pretrained.assert_called_once_with(str(tmp_path), local_files_only=True)
    assert result is sdk.from_pretrained.return_value


def test_private_checkpoint_retains_authorization_without_anonymous_retry(tmp_path):
    download = Mock(side_effect=PermissionError("fixture-denial"))
    sdk = Mock()
    with patch.dict("sys.modules", {
        "huggingface_hub": SimpleNamespace(snapshot_download=download),
        "toto2": SimpleNamespace(Toto2Model=sdk),
    }), pytest.raises(PermissionError):
        load_toto_model("private-org/custom-toto", "a" * 40, token="fixture-credential")
    assert download.call_args.kwargs["token"] == "fixture-credential"
    assert download.call_count == 1
    sdk.from_pretrained.assert_not_called()


@pytest.mark.parametrize("config,weights,missing", [
    (False, True, "configuration"), (True, False, "weights"), (False, False, "configuration"),
])
def test_incomplete_snapshot_is_rejected_without_sdk_or_remote_fallback(tmp_path, config, weights, missing):
    if config:
        (tmp_path / "config.json").write_text("{}")
    if weights:
        (tmp_path / "model.safetensors").write_bytes(b"fixture")
    sdk = Mock()
    with patch.dict("sys.modules", {
        "huggingface_hub": SimpleNamespace(snapshot_download=lambda **kw: str(tmp_path)),
        "toto2": SimpleNamespace(Toto2Model=sdk),
    }), pytest.raises(FileNotFoundError, match=missing):
        load_toto_model("Datadog/Toto-2.0-4m", None)
    sdk.from_pretrained.assert_not_called()


def test_download_failure_cannot_trigger_a_different_checkpoint(tmp_path):
    sdk = Mock()
    with patch.dict("sys.modules", {
        "huggingface_hub": SimpleNamespace(snapshot_download=Mock(side_effect=TimeoutError("fixture"))),
        "toto2": SimpleNamespace(Toto2Model=sdk),
    }), pytest.raises(TimeoutError):
        load_toto_model("Datadog/Toto-2.0-4m", None)
    sdk.from_pretrained.assert_not_called()
