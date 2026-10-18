from __future__ import annotations

import time

import numpy as np

from ..capabilities import MODEL_REGISTRY
from ..schemas import (
    ForecastRequest,
    ModelForecast,
    PreparedSeries,
    canonical_quantile_string,
)
from ..validation import monotonic_rearrangement
from .native_foundation import checkpoint_identity
from .base import (
    ForecastAdapter,
    ModelExecutionError,
    cleanup_memory,
    device_name,
    hf_token,
    package_versions,
)


def chronos_quantile_matrix(raw, quantile_count, prediction_length):
    """Chronos returns variate x horizon x quantile, including square cases."""
    values = np.asarray(raw, dtype=np.float64)
    if values.shape != (1, prediction_length, quantile_count) or not np.isfinite(values).all():
        raise ModelExecutionError("chronos", "INVALID_MODEL_OUTPUT")
    return monotonic_rearrangement(values[0].T)


class ChronosAdapter(ForecastAdapter):
    model_id = "chronos"

    def forecast(
        self,
        series: PreparedSeries,
        timestamps: tuple[str, ...],
        request: ForecastRequest,
    ) -> ModelForecast:
        del timestamps
        started = time.monotonic()
        try:
            import torch
            from chronos import Chronos2Pipeline
        except ImportError as exc:
            raise ModelExecutionError(
                self.model_id, "MODEL_DEPENDENCY_MISSING"
            ) from exc
        pipeline = None
        try:
            checkpoint, revision = checkpoint_identity(self.model_id, request)
            device = device_name("auto")
            pipeline = Chronos2Pipeline.from_pretrained(
                checkpoint, device_map=device, token=hf_token(),
                **({"revision": revision} if revision else {})
            )
            trained = tuple(
                float(value) for value in getattr(pipeline, "quantiles", [])
            )
            if not trained:
                raise ModelExecutionError(self.model_id, "MODEL_CAPABILITY_UNAVAILABLE")
            minimum, maximum = min(trained), max(trained)
            supported = tuple(
                value for value in request.quantiles if minimum <= value <= maximum
            )
            if not supported:
                raise ModelExecutionError(
                    self.model_id, "REQUESTED_QUANTILE_OUTSIDE_TRAINED_RANGE"
                )
            context_length = min(
                request.context_length or 512,
                int(
                    getattr(
                        pipeline,
                        "model_context_length",
                        MODEL_REGISTRY["models"]["chronos"]["maxContextLength"],
                    )
                ),
                len(series.transformed_values),
            )
            context = torch.tensor(
                series.transformed_values[-context_length:], dtype=torch.float32
            )
            with torch.inference_mode():
                quantiles, _point = pipeline.predict_quantiles(
                    inputs=[context],
                    prediction_length=request.prediction_length,
                    quantile_levels=list(supported),
                    batch_size=1,
                    context_length=context_length,
                )
            if len(quantiles) != 1:
                raise ModelExecutionError(self.model_id, "INVALID_MODEL_OUTPUT")
            raw = quantiles[0]
            if torch.is_tensor(raw):
                raw = raw.detach().float().cpu().numpy()
            supported_matrix = chronos_quantile_matrix(raw, len(supported), request.prediction_length)
            matrix = np.full(
                (len(request.quantiles), request.prediction_length), np.nan
            )
            for index, q in enumerate(supported):
                matrix[request.quantiles.index(q)] = supported_matrix[index]
            provenance = {
                canonical_quantile_string(q): (
                    "unavailable"
                    if q not in supported
                    else "native"
                    if q in trained
                    else "interpolated_inside_trained_range"
                )
                for q in request.quantiles
            }
            result = ModelForecast(
                model_id="chronos",
                checkpoint=checkpoint,
                checkpoint_revision=revision,
                prediction_length=request.prediction_length,
                requested_quantiles=request.quantiles,
                available_quantiles=supported,
                quantile_matrix=matrix,
                quantile_provenance=provenance,
                device=device,
                duration_seconds=time.monotonic() - started,
                package_versions=package_versions(["chronos-forecasting", "torch"]),
                warnings=(
                    [
                        "Chronos checkpoint does not support all requested tails; unavailable quantiles are excluded, never clamped."
                    ]
                    if len(supported) < len(request.quantiles)
                    else []
                ),
            )
            result.validate()
            return result
        except ModelExecutionError:
            raise
        except Exception as exc:
            raise ModelExecutionError(
                self.model_id, "MODEL_INFERENCE_FAILED", retryable=True
            ) from exc
        finally:
            if pipeline is not None:
                del pipeline
            cleanup_memory()
