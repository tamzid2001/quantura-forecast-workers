"""Regression coverage for concentrated forecasts and scaled CDF boundaries."""
import math

import pytest

from metaculus_bot.sdk.numeric_report import NumericDistribution, Percentile


def distribution(values, *, lower=0.0, upper=100.0, **kwargs):
    heights = [.01, .1, .25, .5, .75, .9, .99]
    return NumericDistribution(
        declared_percentiles=[Percentile(percentile=p, value=v) for p, v in zip(heights, values)],
        lower_bound=lower, upper_bound=upper, zero_point=None,
        open_lower_bound=True, open_upper_bound=True, **kwargs,
    )


@pytest.mark.parametrize("lower", [1_700_000_000.0, 1e12])
def test_repeated_lower_quantiles_survive_float_precision(lower):
    # The SDK's 1e-10 adjustment rounds back to the same float at these scales.
    d = distribution([lower, lower, lower, lower+25, lower+50, lower+75, lower+100],
                     lower=lower, upper=lower+100)
    assert d._get_cdf_at(0) == .01
    cdf = [p.percentile for p in d.get_cdf()]
    assert len(cdf) == 201
    assert all(math.isfinite(x) and 0 <= x <= 1 for x in cdf)
    assert all(a <= b for a, b in zip(cdf, cdf[1:]))


def test_cdf_at_repeated_value_is_right_continuous():
    d = distribution([-10, 10, 25, 25, 25, 90, 110],
                     strict_validation=False, standardize_cdf=False)
    assert d._get_cdf_at(.25) == pytest.approx(.75)
    assert d._get_cdf_at(.25-1e-7) == pytest.approx(.25, abs=1e-6)
    assert d._get_cdf_at(.25+1e-7) == pytest.approx(.75, abs=1e-6)


@pytest.mark.parametrize("lower,upper", [(1, 1), (10, 1), (0, float('inf')),
                                        (float('nan'), 10)])
def test_invalid_scale_is_rejected_before_interpolation(lower, upper):
    with pytest.raises(ValueError):
        distribution([1, 2, 3, 4, 5, 6, 7], lower=lower, upper=upper)


def test_standardization_rejects_zero_inbound_mass_without_division():
    d = distribution([1, 10, 25, 50, 75, 90, 99])
    d.open_lower_bound = False
    d.open_upper_bound = False
    with pytest.raises(ValueError, match="INBOUND"):
        d._standardize_cdf([.5] * 201)


@pytest.mark.parametrize("kwargs", [{"cdf_size":1}, {"cdf_size":0},
                                    {"zero_point":float('inf')}])
def test_invalid_cdf_configuration_fails_validation(kwargs):
    args=dict(declared_percentiles=[Percentile(percentile=.1,value=10),
                                   Percentile(percentile=.9,value=90)],
              lower_bound=0,upper_bound=100,zero_point=None,
              open_lower_bound=True,open_upper_bound=True)
    with pytest.raises(ValueError):
        NumericDistribution(**{**args,**kwargs})


def test_nonfinite_value_and_log_scaled_ties():
    with pytest.raises(ValueError,match='FINITE_QUANTILES'):
        distribution([1,10,25,50,75,90,float('inf')])
    d=NumericDistribution(
        declared_percentiles=[Percentile(percentile=p,value=v) for p,v in zip(
            [.01,.1,.25,.5,.75,.9,.99],[1,1,1,100,1000,10000,100000])],
        lower_bound=1,upper_bound=1e6,zero_point=0,
        open_lower_bound=True,open_upper_bound=True)
    cdf=d.get_cdf()
    assert len(cdf)==201
    assert all(a.percentile<=b.percentile for a,b in zip(cdf,cdf[1:]))


@pytest.mark.parametrize('location', [float('nan'),float('inf'),-2,2])
def test_invalid_or_outside_interpolation_location(location):
    with pytest.raises(ValueError,match='CDF_INTERPOLATION'):
        distribution([1,10,25,50,75,90,99])._get_cdf_at(location)


def test_interpolation_tolerates_only_endpoint_rounding():
    d=distribution([1,10,25,50,75,90,99],standardize_cdf=False)
    # Explicit open-tail anchors are exactly at 0 and 1 for this distribution.
    assert d._get_cdf_at(-1e-12)==pytest.approx(.005)
    assert d._get_cdf_at(1+1e-12)==pytest.approx(.995)


def test_representative_and_inverse_quantiles_remain_consistent():
    d=distribution([1,10,25,50,75,90,99])
    assert [p.value for p in d.get_percentiles_at_target_heights([.2,.3])]==pytest.approx([20,30])
    assert len(d.get_representative_percentiles(3))==3
    assert len(d.get_representative_percentiles(100))==7
    with pytest.raises(ValueError):d.get_representative_percentiles(1)
