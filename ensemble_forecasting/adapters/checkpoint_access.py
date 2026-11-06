"""Explicit download policy for inspected public checkpoints.

These exact model cards permit public download. Custom and private model IDs
retain caller authentication; denied access never causes anonymous retries.
This policy does not modify any model-license or production capability gate.
"""
from pathlib import Path

PUBLIC_CHECKPOINTS = frozenset({
    "Datadog/Toto-2.0-4m",
    "ibm-granite/granite-timeseries-patchtst-fm-r2",
    "amazon/chronos-2",
})


def checkpoint_token(checkpoint: str, token: str | None) -> str | bool | None:
    return False if checkpoint in PUBLIC_CHECKPOINTS else token


def download_safetensors_snapshot(checkpoint: str, revision: str | None, *, token: str | None = None) -> str:
    from huggingface_hub import snapshot_download

    snapshot = Path(snapshot_download(
        repo_id=checkpoint, revision=revision, token=checkpoint_token(checkpoint, token),
        allow_patterns=["config.json", "*.safetensors", "*.safetensors.index.json"],
    ))
    if not (snapshot / "config.json").is_file():
        raise FileNotFoundError("Checkpoint snapshot configuration is missing")
    if not any((snapshot / name).is_file() for name in (
        "model.safetensors", "model.safetensors.index.json"
    )):
        raise FileNotFoundError("Checkpoint snapshot safetensors weights are missing")
    return str(snapshot)


def checkpoint_location(checkpoint: str, revision: str | None, *, token: str | None = None) -> str:
    if checkpoint in PUBLIC_CHECKPOINTS:
        return download_safetensors_snapshot(checkpoint, revision, token=token)
    return checkpoint
