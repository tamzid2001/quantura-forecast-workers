from __future__ import annotations

import time

import numpy as np

from ..capabilities import MODEL_REGISTRY
from ..schemas import ForecastRequest, ModelForecast, PreparedSeries
from .base import ForecastAdapter, ModelExecutionError, cleanup_memory, device_name, hf_token, package_versions
from .native_foundation import checkpoint_identity, native_output


class TimestralAdapter(ForecastAdapter):
    model_id = "timestral"

    def forecast(self, series: PreparedSeries, timestamps: tuple[str, ...], request: ForecastRequest) -> ModelForecast:
        del timestamps
        started = time.monotonic()
        try:
            import torch
            from timestral import TimestralForecaster
        except ImportError as exc:
            raise ModelExecutionError(self.model_id, "MODEL_DEPENDENCY_MISSING") from exc
        model = None
        try:
            checkpoint, revision = checkpoint_identity(self.model_id, request)
            device = device_name("auto")
            length = min(request.context_length or 512, len(series.transformed_values),
                         MODEL_REGISTRY["models"][self.model_id]["maxContextLength"])
            model = TimestralForecaster.from_pretrained(checkpoint, revision=revision, token=hf_token(),
                                                        device=device, max_context=length)
            with torch.inference_mode():
                output = model.forecast(np.asarray(series.transformed_values[-length:], dtype=np.float32),
                                        prediction_length=request.prediction_length, clip_nonnegative=False)
            # Official API: B x V x H x Q. Preserve orientation even when H == Q.
            raw = output.quantiles.detach().float().cpu().numpy()
            levels = tuple(float(q) for q in output.quantile_levels)
            if raw.shape != (1, 1, request.prediction_length, len(levels)):
                raise ValueError("invalid Timestral batch/variate shape")
            matrix, available, provenance = native_output(raw[0, 0].T, levels, request)
            result = ModelForecast(
                model_id=self.model_id, checkpoint=checkpoint, checkpoint_revision=revision,
                prediction_length=request.prediction_length, requested_quantiles=request.quantiles,
                available_quantiles=available, quantile_matrix=matrix, quantile_provenance=provenance,
                device=device, duration_seconds=time.monotonic() - started,
                package_versions=package_versions(["timestral", "torch", "huggingface-hub"]),
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
