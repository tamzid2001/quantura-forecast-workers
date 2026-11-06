"""Offline loader control tests; real model execution is a separate diagnostic."""
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from ensemble_forecasting.adapters.toto_loader import load_toto_model


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
        repo_id="Datadog/Toto-2.0-4m", revision=revision, token="fixture-credential",
        allow_patterns=["config.json", "*.safetensors", "*.safetensors.index.json"],
    )
    sdk.from_pretrained.assert_called_once_with(str(tmp_path), local_files_only=True)
    assert result is sdk.from_pretrained.return_value


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
