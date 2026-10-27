"""Cost-aware uncertainty estimates. No annualization or quantile-to-win conversion."""
from __future__ import annotations

import math
import random
import statistics as stdlib
from collections import defaultdict

from .data import day


def quantile(values, p):
    if (not values or isinstance(p, bool) or not 0 <= p <= 1
            or any(not math.isfinite(v) for v in values)):
        raise ValueError("INVALID_QUANTILE")
    ordered = sorted(values)
    rank = (len(ordered) - 1) * p
    lo = math.floor(rank)
    hi = math.ceil(rank)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (rank - lo)


def wilson(wins, total, z=1.959963984540054):
    if (type(wins) is not int or type(total) is not int
            or not 0 <= wins <= total or total < 0):
        raise ValueError("INVALID_BINOMIAL_COUNTS")
    if total == 0:
        return None
    p = wins / total
    scale = 1 + z * z / total
    center = (p + z * z / (2 * total)) / scale
    radius = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / scale
    return [max(0.0, center - radius), min(1.0, center + radius)]


def sign_p_value(values):
    """One-sided exact sign test; tests positive median, NOT positive mean P&L."""
    if any(not math.isfinite(v) for v in values):
        raise ValueError("INVALID_SIGN_TEST_VALUE")
    nonzero = [v for v in values if abs(v) > 1e-12]
    n, k = len(nonzero), sum(v > 0 for v in nonzero)
    if not n:
        return None
    if k == 0:
        return 1.0
    terms = [math.exp(math.lgamma(n + 1) - math.lgamma(i + 1)
                      - math.lgamma(n - i + 1) - n * math.log(2)) for i in range(k, n + 1)]
    return min(1.0, math.fsum(terms))


def bonferroni(p, tests):
    if isinstance(tests, bool) or not isinstance(tests, int) or tests < 1:
        raise ValueError("INVALID_TEST_FAMILY")
    if p is None:
        return None
    if not 0 <= p <= 1:
        raise ValueError("INVALID_P_VALUE")
    return min(1.0, p * tests)


def daily(trades, extra_per_contract=0):
    groups = defaultdict(lambda: {"profit": 0.0, "spend": 0.0, "n": 0})
    for t in trades:
        if t.closed:
            v = groups[day(t.entry_at)]
            v["profit"] += float(t.profit(extra_per_contract))
            v["spend"] += float(t.spend) + float(t.quantity) * extra_per_contract
            v["n"] += 1
    return dict(sorted(groups.items()))


def blocks(values, length=3):
    return [math.fsum(values[i:i + length]) for i in range(0, len(values), length)]


def block_indices(n, draws, seed, length=3):
    rng = random.Random(seed)
    for _ in range(draws):
        result = []
        while len(result) < n:
            start = rng.randrange(n)
            result.extend((start + j) % n for j in range(min(length, n)))
        yield result[:n]


def bootstrap_comparison(baseline, selected, draws=2000, seed=20261027, min_days=8):
    """Paired three-trading-day moving blocks retain same-day game correlations.

    These exploratory percentile intervals are pointwise, not simultaneous.
    The observed cohort is not asserted to be representative or stationary.
    """
    if not 100 <= draws <= 10000:
        raise ValueError("BOOTSTRAP_DRAWS_OUT_OF_BOUNDS")
    b, s = daily(baseline), daily(selected)
    dates = sorted(b)
    if len(dates) < min_days:
        return {"status": "insufficient_trading_days", "days": len(dates), "roi_ci95_pct": None,
                "mean_daily_improvement_ci95_usd": None}
    profits = [s.get(d, {}).get("profit", 0.0) for d in dates]
    spends = [s.get(d, {}).get("spend", 0.0) for d in dates]
    deltas = [profits[i] - b[d]["profit"] for i, d in enumerate(dates)]
    roi, improvement, zero = [], [], 0
    for indices in block_indices(len(dates), draws, seed):
        spend = math.fsum(spends[i] for i in indices)
        if spend <= 0:
            zero += 1
        else:
            roi.append(100 * math.fsum(profits[i] for i in indices) / spend)
        improvement.append(math.fsum(deltas[i] for i in indices) / len(dates))
    enough = zero <= draws * 0.05 and len(roi) >= draws * 0.95
    status = "estimated" if enough else "insufficient_resamples_with_exposure"
    if enough and max(roi) - min(roi) < 1e-12:
        status = "degenerate_roi_bootstrap"
    return {"status": status,
            "days": len(dates), "draws": draws, "seed": seed, "block_length": min(3, len(dates)),
            "zero_exposure_resamples": zero,
            "roi_ci95_pct": [quantile(roi, .025), quantile(roi, .975)] if enough else None,
            "mean_daily_improvement_ci95_usd": [quantile(improvement, .025), quantile(improvement, .975)],
            "interval_scope": "pointwise exploratory percentile intervals; observed trading-day blocks"}


def summary(trades, extra_per_contract=0):
    if isinstance(extra_per_contract, bool) or not math.isfinite(extra_per_contract) or not 0 <= extra_per_contract <= 1:
        raise ValueError("INVALID_STRESS_COST")
    settled = sorted((t for t in trades if t.closed), key=lambda t: (t.settled_at, t.fixture))
    pnl = [float(t.profit(extra_per_contract)) for t in settled]
    returns = [p / (float(t.spend) + float(t.quantity) * extra_per_contract) for p, t in zip(pnl, settled)]
    win, loss = sum(p > 1e-12 for p in pnl), sum(p < -1e-12 for p in pnl)
    spend = math.fsum(float(t.spend) + float(t.quantity) * extra_per_contract for t in settled)
    gross_win = math.fsum(max(p, 0) for p in pnl)
    gross_loss = -math.fsum(min(p, 0) for p in pnl)
    equity = peak = dd = 0.0
    run_w = run_l = max_w = max_l = 0
    for p in pnl:
        equity += p
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
        run_w = run_w + 1 if p > 1e-12 else 0
        run_l = run_l + 1 if p < -1e-12 else 0
        max_w, max_l = max(max_w, run_w), max(max_l, run_l)
    days = daily(settled, extra_per_contract)
    day_profits = [v["profit"] for v in days.values()]
    block_profits = blocks(day_profits)
    binary = [t for t in settled if t.payout in (0, 1)]
    brier = [float((t.planned_price - t.payout) ** 2) for t in binary]
    quantity_total = math.fsum(float(t.quantity) for t in settled)
    payout_total = math.fsum(float(t.quantity * t.payout) for t in settled)
    binary_log = [-math.log(max(1e-12, float(t.planned_price if t.payout == 1 else 1 - t.planned_price))) for t in binary]
    variance = stdlib.variance(pnl) if len(pnl) >= 2 else None
    ret_std = stdlib.stdev(returns) if len(returns) >= 2 else None
    tail = sorted(returns)[:max(1, math.ceil(.05 * len(returns)))] if returns else []
    return {"entries": len(trades), "settled": len(settled), "open": len(trades) - len(settled),
            "wins": win, "losses": loss, "breakevens": len(pnl) - win - loss,
            "win_rate_pct": 100 * win / len(pnl) if pnl else None,
            "win_rate_iid_wilson95": wilson(win, len(pnl)),
            "net_pnl_usd": math.fsum(pnl), "fees_usd": math.fsum(float(t.fee) for t in settled),
            "closed_spend_usd": spend,
            "cashflow_break_even_payout_fraction": spend / quantity_total if quantity_total else None,
            "cashflow_weighted_actual_payout_fraction": payout_total / quantity_total if quantity_total else None,
            "roi_pct": 100 * math.fsum(pnl) / spend if spend else None,
            "mean_net_per_game_usd": stdlib.mean(pnl) if pnl else None,
            "variance_net_per_game_usd2": variance,
            "std_net_per_game_usd": math.sqrt(variance) if variance is not None else None,
            "return_std": ret_std,
            "mean_return_over_std_not_annualized": stdlib.mean(returns) / ret_std if ret_std else None,
            "empirical_return_q05": quantile(returns, .05) if returns else None,
            "empirical_worst_5pct_mean_return": stdlib.mean(tail) if tail else None,
            "profit_factor": gross_win / gross_loss if gross_loss else None,
            "max_realized_drawdown_usd": dd, "max_win_streak": max_w, "max_loss_streak": max_l,
            "trading_days": len(days), "daily_net_usd": days,
            "block_sign_p_positive_median": sign_p_value(block_profits),
            "sign_test_nonoverlapping_blocks": len(block_profits),
            "sign_test_null": "median profit of independent nonzero three-trading-day blocks <= 0; not a mean-profit test",
            "mean_break_even_win_probability": stdlib.mean(float(t.spend / t.quantity) + extra_per_contract for t in settled) if settled else None,
            "entry_quote_brier_baseline": stdlib.mean(brier) if brier else None,
            "entry_quote_log_loss_baseline": stdlib.mean(binary_log) if binary_log else None,
            "binary_calibration_n": len(binary),
            "calibration_note": "entry quote baseline only; P01 is not a settlement probability; cancelled/fractional payouts excluded",
            "stress_extra_cost_per_contract_usd": extra_per_contract}


def independent_sample_planning(sigma, effect_usd=.05, alpha=.05, power=.8):
    """Approximate IID normal-mean sample size, not achieved power or a guarantee."""
    if (not all(math.isfinite(v) for v in (sigma, effect_usd, alpha, power))
            or sigma <= 0 or effect_usd <= 0 or not 0 < alpha < 1 or not .5 < power < 1):
        raise ValueError("INVALID_SAMPLE_SIZE_ASSUMPTIONS")
    normal = stdlib.NormalDist()
    n = math.ceil(((normal.inv_cdf(1-alpha/2)+normal.inv_cdf(power))*sigma/effect_usd)**2)
    return {"approximate_independent_games": n, "target_mean_improvement_usd": effect_usd,
            "sigma_usd": sigma, "alpha": alpha, "power": power,
            "assumptions": "IID normal-mean planning only; clustering, selection, skew and multiple tests can require more evidence"}
