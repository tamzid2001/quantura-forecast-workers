"""Explicitly assumed interpolation, with unidentified tails kept as bounds."""
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class EventProbability:
    point: float | None
    lower: float
    upper: float
    method: str


def threshold_probability(points, threshold, operator, *, assume_continuous=False):
    """Estimate P(X>T) or P(X<T), never a path-dependent touching probability.

    Quantiles alone identify probability bounds. An interior point estimate
    additionally assumes a continuous, piecewise-linear CDF. No tail density is
    invented. Repeated quantile values require a separate atom/mass model.
    """
    if operator not in {"above", "below"}:
        raise ValueError("POINT_THRESHOLD_OPERATOR_REQUIRED")
    if isinstance(threshold, bool) or not math.isfinite(float(threshold)):
        raise ValueError("FINITE_THRESHOLD_REQUIRED")
    threshold = float(threshold)
    probabilities, values = [], []
    for point in points:
        p, value = point["percentile"], point["value"]
        if isinstance(p, bool) or isinstance(value, bool):
            raise ValueError("NUMERIC_QUANTILES_REQUIRED")
        p, value = float(p), float(value)
        if not math.isfinite(p) or not math.isfinite(value) or not 0 <= p <= 1:
            raise ValueError("FINITE_QUANTILES_REQUIRED")
        probabilities.append(p)
        values.append(value)
    if len(values) < 2 or any(b <= a for a, b in zip(probabilities, probabilities[1:])):
        raise ValueError("ORDERED_QUANTILE_PROBABILITIES_REQUIRED")
    if any(b < a for a, b in zip(values, values[1:])):
        raise ValueError("NONCROSSING_QUANTILES_REQUIRED")
    if assume_continuous and len(set(values)) != len(values):
        raise ValueError("QUANTILE_TIES_REQUIRE_MASS_MODEL")
    # Strict below uses F(T-); strict above uses 1-F(T). This distinction
    # matters when the distribution can have atoms at the threshold.
    index = bisect_right(values, threshold) if operator == "above" else bisect_left(values, threshold)
    low = probabilities[index - 1] if index else 0.0
    high = probabilities[index] if index < len(values) else 1.0
    point = None
    if assume_continuous and values[0] <= threshold <= values[-1]:
        right = bisect_left(values, threshold)
        if values[right] == threshold:
            point = probabilities[right]
        else:
            left = right - 1
            weight = (threshold - values[left]) / (values[right] - values[left])
            point = probabilities[left] + weight * (probabilities[right] - probabilities[left])
        low = high = point
    if operator == "above":
        low, high = 1 - high, 1 - low
        point = None if point is None else 1 - point
    return EventProbability(point, low, high,
                            "continuous_piecewise_linear_cdf" if point is not None else "quantile_probability_bounds")


def reviewed_binary_event(spec):
    """Bind the exact variable, native units, observation date and point event."""
    event = spec.get("binary_event")
    variable = spec.get("series_id") or spec.get("value_column")
    if not isinstance(event, dict) or not variable:
        return None
    if (event.get("variable") != variable or event.get("unit") != spec.get("unit")
            or event.get("target_date") != spec.get("target_date")
            or event.get("aggregation") != "point_in_time"
            or event.get("distribution") != "continuous"
            or event.get("operator") not in {"above", "below"}):
        return None
    threshold = event.get("threshold")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(threshold):
        return None
    return event
