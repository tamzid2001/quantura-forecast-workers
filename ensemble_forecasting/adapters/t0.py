from __future__ import annotations

import time

import numpy as np

from ..capabilities import MODEL_REGISTRY
from ..schemas import ForecastRequest, ModelForecast, PreparedSeries
from .base import ForecastAdapter, ModelExecutionError, cleanup_memory, device_name, hf_token, package_versions
from .native_foundation import checkpoint_identity, native_output


class T0Adapter(ForecastAdapter):
    model_id = "t0"

    def forecast(self, series: PreparedSeries, timestamps: tuple[str, ...], request: ForecastRequest) -> ModelForecast:
        del timestamps
        started = time.monotonic()
        try:
            import torch
            from t0 import T0Forecaster
        except ImportError as exc:
            raise ModelExecutionError(self.model_id, "MODEL_DEPENDENCY_MISSING") from exc
        model = None
        try:
            checkpoint, revision = checkpoint_identity(self.model_id, request)
            device = device_name("auto")
            registry = MODEL_REGISTRY["models"][self.model_id]
            levels = tuple(registry["quantileSupport"]["native"])
            length = min(request.context_length or 512, len(series.transformed_values), registry["maxContextLength"])
            model = T0Forecaster.from_pretrained(checkpoint, revision=revision, token=hf_token()).to(device).eval()
            context = np.asarray(series.transformed_values[-length:], dtype=np.float32)[None, :]
            with torch.inference_mode():
                # Request native knots, then interpolate inside their range only.
                # T0's optional exponential tail extrapolation is never used.
                options = ({"symmetric_averaging": True}
                           if request.model_policies.get(self.model_id) == "frequency_aware_v2" else {})
                output = model.predict(context, horizon=request.prediction_length,
                                       quantile_levels=list(levels), **options)
            raw = output.quantiles.detach().float().cpu().numpy()
            if raw.shape != (1, request.prediction_length, len(levels)):
                raise ValueError("invalid T0 batch shape")
            matrix, available, provenance = native_output(raw[0].T, levels, request)
            result = ModelForecast(
                model_id=self.model_id, checkpoint=checkpoint, checkpoint_revision=revision,
                prediction_length=request.prediction_length, requested_quantiles=request.quantiles,
                available_quantiles=available, quantile_matrix=matrix, quantile_provenance=provenance,
                device=device, duration_seconds=time.monotonic() - started,
                package_versions=package_versions(["tfc-t0", "torch", "huggingface-hub"]),
            )
            result.validate()
            return result
        except ModelExecutionError:
            raise
        except Exception as exc:
            raise ModelExecutionError(self.model_id, "MODEL_INFERENCE_FAILED", retryable=True) from exc
        finally:
            if model is not None:
                del model
            cleanup_memory()
