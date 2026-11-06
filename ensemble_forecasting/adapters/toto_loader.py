"""Load the pinned Toto 2 SDK through its supported local-snapshot path.

The 2.0.0 remote Hub mixin does not construct Toto2ModelConfig correctly with
the pinned hub dependency. Its local loader explicitly constructs that object.
Only configuration and safetensors are downloaded; no repository code executes.
"""
from pathlib import Path

PUBLIC_CHECKPOINT = "Datadog/Toto-2.0-4m"


def load_toto_model(checkpoint: str, revision: str | None, *, token: str | None = None):
    from huggingface_hub import snapshot_download
    from toto2 import Toto2Model

    snapshot = Path(snapshot_download(
        repo_id=checkpoint,
        revision=revision,
        # This inspected Apache-2.0 checkpoint is public and needs no account.
        # Explicit False prevents an expired ambient HF_TOKEN from breaking it.
        # Custom/private checkpoints retain their supplied authentication.
        token=False if checkpoint == PUBLIC_CHECKPOINT else token,
        allow_patterns=["config.json", "*.safetensors", "*.safetensors.index.json"],
    ))
    if not (snapshot / "config.json").is_file():
        raise FileNotFoundError("Toto snapshot configuration is missing")
    if not any((snapshot / name).is_file() for name in (
        "model.safetensors", "model.safetensors.index.json"
    )):
        raise FileNotFoundError("Toto snapshot safetensors weights are missing")
    # The SDK's local path builds its dataclass and loads weights strictly.
    # A partial snapshot must never fall back to a different remote checkpoint.
    return Toto2Model.from_pretrained(str(snapshot), local_files_only=True)
