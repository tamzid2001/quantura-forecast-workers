"""Features from information available by the signal, never future outcomes."""
from __future__ import annotations

import itertools
import math
import statistics

FIELDS = ("quote_change_std", "quote_change_variance", "quote_change_pairs", "feature_cutoff_at",
          "forecast_iqr", "forecast_tail_width", "discount_quote_sigma", "entry_spread")


def finite(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def capture(points, signal_at, p01, signal_ask, quantiles=None):
    """Sample variance of consecutive-minute mid-quote changes in the prior hour.

    Missing minutes are not bridged. Twenty adjacent minute pairs are required;
    zero variance remains zero and its standardized discount remains undefined.
    This sigma is a price-change feature, not a normal-distribution win model.
    """
    indexed, conflicts = {}, set()
    for point in points:
        at, bid, ask = (finite(point.get(k)) for k in ("at", "bid", "ask"))
        if at is None or bid is None or ask is None or not signal_at - 3600 < at <= signal_at:
            continue
        if not 0 <= bid <= ask <= 1:
            continue
        mid = (bid + ask) / 2
        if at in indexed and indexed[at] != mid:
            conflicts.add(at)
        indexed[at] = mid
    for at in conflicts:
        indexed.pop(at, None)
    ordered = sorted(indexed.items())
    changes = [right[1] - left[1] for left, right in itertools.pairwise(ordered)
               if abs(right[0] - left[0] - 60) < 1e-6]
    variance = statistics.variance(changes) if len(changes) >= 20 else None
    sigma = math.sqrt(variance) if variance is not None else None
    result = {"quote_change_pairs": len(changes), "quote_change_variance": variance,
              "quote_change_std": sigma, "feature_cutoff_at": signal_at,
              "discount_quote_sigma": (p01 - signal_ask) / sigma if sigma else None,
              "forecast_iqr": None, "forecast_tail_width": None}
    qs = {}
    for key, value in (quantiles or {}).items():
        level, price = finite(key), finite(value)
        if level is not None and price is not None and 0 < level < 1 and 0 <= price <= 1:
            qs[level] = price
    ordered_q = sorted(qs)
    if all(qs[a] <= qs[b] for a, b in itertools.pairwise(ordered_q)):
        if .25 in qs and .75 in qs:
            result["forecast_iqr"] = qs[.75] - qs[.25]
        if .01 in qs and .99 in qs:
            result["forecast_tail_width"] = qs[.99] - qs[.01]
    return result
