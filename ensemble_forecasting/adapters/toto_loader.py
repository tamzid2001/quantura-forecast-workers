"""Load the pinned Toto 2 SDK through its supported local-snapshot path.

The 2.0.0 remote Hub mixin does not construct Toto2ModelConfig correctly with
the pinned hub dependency. Its local loader explicitly constructs that object.
Only configuration and safetensors are downloaded; no repository code executes.
"""
from .checkpoint_access import download_safetensors_snapshot


def load_toto_model(checkpoint: str, revision: str | None, *, token: str | None = None):
    from toto2 import Toto2Model

    snapshot = download_safetensors_snapshot(checkpoint, revision, token=token)
    # The SDK's local path builds its dataclass and loads weights strictly.
    # A partial snapshot must never fall back to a different remote checkpoint.
    return Toto2Model.from_pretrained(snapshot, local_files_only=True)
