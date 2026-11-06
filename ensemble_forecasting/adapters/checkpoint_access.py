"""Explicit download policy for inspected public checkpoints.

These exact model cards permit public download. Custom and private model IDs
retain caller authentication; denied access never causes anonymous retries.
This policy does not modify any model-license or production capability gate.
"""
PUBLIC_CHECKPOINTS = frozenset({
    "Datadog/Toto-2.0-4m",
    "ibm-granite/granite-timeseries-patchtst-fm-r2",
})


def checkpoint_token(checkpoint: str, token: str | None) -> str | bool | None:
    return False if checkpoint in PUBLIC_CHECKPOINTS else token
