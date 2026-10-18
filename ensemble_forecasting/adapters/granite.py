from __future__ import annotations

import time

import numpy as np

from ..capabilities import MODEL_REGISTRY
from ..schemas import ForecastRequest, ModelForecast, PreparedSeries, canonical_quantile_string
from ..validation import monotonic_rearrangement
from .native_foundation import checkpoint_identity
from .base import ForecastAdapter, ModelExecutionError, cleanup_memory, hf_token, package_versions


class GraniteAdapter(ForecastAdapter):
    model_id = "granite"

    def forecast(self, series: PreparedSeries, timestamps: tuple[str, ...], request: ForecastRequest) -> ModelForecast:
        del timestamps
        started = time.monotonic()
        try:
            import torch
            from tsfm_public import PatchTSTFMForPrediction
        except ImportError as exc:
            raise ModelExecutionError(self.model_id, "MODEL_DEPENDENCY_MISSING") from exc
        model = None
        try:
            checkpoint, revision = checkpoint_identity(self.model_id, request)
            model = PatchTSTFMForPrediction.from_pretrained(
                checkpoint,
                token=hf_token(),
                low_cpu_mem_usage=True,
                **({"revision": revision} if revision else {}),
            ).to("cpu").eval()
            maximum = int(getattr(model.config, "context_length", MODEL_REGISTRY["models"]["granite"]["maxContextLength"]))
            context_length = min(request.context_length or 512, maximum, len(series.transformed_values))
            values = torch.tensor(series.transformed_values[-context_length:], dtype=torch.float32, device="cpu").unsqueeze(0)
            with torch.inference_mode():
                output = model(
                    past_values=values,
                    prediction_length=request.prediction_length,
                    quantile_levels=list(request.quantiles),
                )
            per_batch = isinstance(output.quantile_outputs, (list, tuple))
            if per_batch and len(output.quantile_outputs) != 1:
                raise ModelExecutionError(self.model_id, "INVALID_MODEL_OUTPUT")
            raw = output.quantile_outputs[0] if per_batch else output.quantile_outputs
            if torch.is_tensor(raw):
                raw = raw.detach().float().cpu().numpy()
            # PatchTST-FM is batch x quantile x horizon x feature; univariate
            # releases may omit the final singleton feature, never swap Q/H.
            raw = np.asarray(raw, dtype=np.float64)
            expected = (len(request.quantiles), request.prediction_length)
            if not per_batch:
                expected = (1, *expected)
            if raw.shape == (*expected, 1):
                raw = raw[..., 0]
            if raw.shape != expected or not np.isfinite(raw).all():
                raise ModelExecutionError(self.model_id, "INVALID_MODEL_OUTPUT")
            matrix = monotonic_rearrangement(raw if per_batch else raw[0])
            result = ModelForecast(
                model_id="granite",
                checkpoint=checkpoint,
                checkpoint_revision=revision,
                prediction_length=request.prediction_length,
                requested_quantiles=request.quantiles,
                available_quantiles=request.quantiles,
                quantile_matrix=matrix,
                quantile_provenance={canonical_quantile_string(q): "requested_model_quantile" for q in request.quantiles},
                device="cpu",
                duration_seconds=time.monotonic() - started,
                package_versions=package_versions(["granite-tsfm", "torch"]),
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
