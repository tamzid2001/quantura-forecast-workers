# Retained from forecasting-tools 0.3.4 (MIT); see LICENSE and provenance.json.
from abc import ABC
from datetime import datetime

from pydantic import BaseModel

from .binary_prediction import BinaryPrediction
from .numeric_report import NumericDistribution


class TimeStampedPrediction(BaseModel, ABC):
    timestamp: datetime
    timestamp_end: datetime | None


class BinaryTimestampedPrediction(BinaryPrediction, TimeStampedPrediction):
    pass


class NumericTimestampedDistribution(NumericDistribution, TimeStampedPrediction):
    strict_validation: bool = False
