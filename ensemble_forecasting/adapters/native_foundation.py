"""Shared boundaries for the two 21-quantile foundation-model adapters."""
from __future__ import annotations

import numpy as np

from ..capabilities import MODEL_REGISTRY
from ..schemas import ForecastRequest
from ..validation import interpolate_native_quantiles


def checkpoint_identity(model_id: str, request: ForecastRequest) -> tuple[str, str | None]:
    registry = MODEL_REGISTRY["models"][model_id]
    checkpoint = request.model_checkpoints.get(model_id) or registry["checkpoint"]
    revision = request.model_revisions.get(model_id) or (
        registry.get("checkpointRevision") if checkpoint == registry["checkpoint"] else None
    )
    return checkpoint, revision


def native_output(raw: np.ndarray, levels: tuple[float, ...], request: ForecastRequest):
    """Accept an explicitly oriented Q x H matrix; never infer square axes."""
    values = np.asarray(raw, dtype=np.float64)
    if (values.shape != (len(levels), request.prediction_length)
            or not np.isfinite(values).all() or len(levels) < 2
            or not all(0 < q < 1 for q in levels) or np.any(np.diff(levels) <= 0)):
        raise ValueError("invalid native quantile output")
    return interpolate_native_quantiles(values, native_levels=levels, requested_levels=request.quantiles)
