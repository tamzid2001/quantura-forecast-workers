import copy
import math

import pytest

from metaculus_bot.probability import reviewed_binary_event, threshold_probability


POINTS = [{"percentile": .1, "value": 2.2}, {"percentile": .5, "value": 2.8},
          {"percentile": .9, "value": 3.4}]


def test_quantiles_become_event_probability_only_under_explicit_assumption():
    bounds = threshold_probability(POINTS, 3, "above")
    assert bounds.point is None and bounds.lower == pytest.approx(.1) and bounds.upper == pytest.approx(.5)
    interpolated = threshold_probability(POINTS, 3, "above", assume_continuous=True)
    assert interpolated.point == pytest.approx(1 - (.5 + .2 / .6 * .4))
    assert interpolated.lower == interpolated.upper == interpolated.point


@pytest.mark.parametrize("threshold,expected", [(2.2, .9), (2.8, .5), (3.4, .1)])
def test_continuous_quantile_anchors_and_complement(threshold, expected):
    above = threshold_probability(POINTS, threshold, "above", assume_continuous=True)
    below = threshold_probability(POINTS, threshold, "below", assume_continuous=True)
    assert above.point == pytest.approx(expected)
    assert above.point + below.point == pytest.approx(1)


@pytest.mark.parametrize("threshold,bounds", [(1, (.9, 1)), (4, (0, .1))])
def test_unobserved_tails_remain_bounds(threshold, bounds):
    result = threshold_probability(POINTS, threshold, "above", assume_continuous=True)
    assert result.point is None
    assert (result.lower, result.upper) == pytest.approx(bounds)


def test_atoms_preserve_distinction_between_strict_above_and_below():
    points = [{"percentile": .1, "value": 1}, {"percentile": .9, "value": 1}]
    above = threshold_probability(points, 1, "above")
    below = threshold_probability(points, 1, "below")
    assert (above.lower, above.upper) == pytest.approx((0, .1))
    assert (below.lower, below.upper) == pytest.approx((0, .1))
    with pytest.raises(ValueError, match="MASS_MODEL"):
        threshold_probability(points, 1, "above", assume_continuous=True)


@pytest.mark.parametrize("points,threshold,operator", [([], 1, "above"),
    (POINTS[::-1], 1, "above"), ([POINTS[1], POINTS[1]], 1, "above"),
    ([{"percentile": .1, "value": 2}, {"percentile": .9, "value": 1}], 1, "above"),
    (POINTS, math.nan, "above"), (POINTS, True, "above"), (POINTS, 3, "ever_touches")])
def test_invalid_or_path_dependent_events_are_rejected(points, threshold, operator):
    with pytest.raises(ValueError):
        threshold_probability(points, threshold, operator, assume_continuous=True)


def binding():
    return {"series_id": "CPI_EXACT_SERIES", "unit": "percentage points", "target_date": "2027-01-01",
            "binary_event": {"variable": "CPI_EXACT_SERIES", "unit": "percentage points", "target_date": "2027-01-01",
                             "aggregation": "point_in_time", "distribution": "continuous", "operator": "above", "threshold": 3}}


@pytest.mark.parametrize("field,value", [("variable", "OTHER_SERIES"), ("unit", "percent change"),
    ("target_date", "2027-02-01"), ("aggregation", "ever_touches"), ("distribution", "discrete"),
    ("operator", "at_least"), ("threshold", math.inf), ("threshold", True)])
def test_exact_event_mapping_cannot_silently_change_resolution_semantics(field, value):
    spec = binding()
    assert reviewed_binary_event(spec) == spec["binary_event"]
    bad = copy.deepcopy(spec); bad["binary_event"][field] = value
    assert reviewed_binary_event(bad) is None
