# Retained from forecasting-tools 0.3.4 (MIT); see LICENSE and provenance.json.
from __future__ import annotations
import logging
from pydantic import BaseModel, field_validator
logger = logging.getLogger(__name__)

class BinaryPrediction(BaseModel):
    prediction_in_decimal: float

    @field_validator("prediction_in_decimal")
    @classmethod
    def validate_prediction_range(cls, value: float) -> float:
        # Adjust the prediction when 0 or 1 since AI making this model sometimes gives 0 or 1.
        if 0.001 <= value <= 0.999:
            return value
        elif 0 <= value < 0.001:
            logger.warning(
                f"Prediction is less than 0.001, adjusting to 0.001. Value: {value}"
            )
            return 0.001
        elif 0.999 < value <= 1:
            logger.warning(
                f"Prediction is greater than 0.999, adjusting to 0.999. Value: {value}"
            )
            return 0.999
        else:
            raise ValueError("Prediction must be between 0 and 1")
