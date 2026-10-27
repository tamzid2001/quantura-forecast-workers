"""Completed-hour above-P99 opposite-side quote replay, with no order client.

This is deliberately independent of P01-selected ledgers. Seven participating
models do not imply that all seven supply the extreme quantile: native tail
availability remains in the original model provenance.
"""
from __future__ import annotations

import hashlib
import itertools
import json
from collections import Counter
from decimal import ROUND_DOWN

from ..game_p01_portfolio import fee
from .data import P99, decimal, normalize, timestamp
from .features import capture, finite

MODELS = {"prophet", "toto", "granite", "chronos", "timesfm", "timestral", "t0"}
SIDES = {"kalshi": {"yes": "no", "no": "yes"},
         "polymarket_us": {"long": "short", "short": "long"}}


def forecast_hours(result):
    forecast = result["forecast"]
    completed = [m["id"] for m in forecast["models"] if m["status"] == "completed"]
    if (set(completed) != MODELS or len(completed) != 7
            or forecast.get("weighting", {}).get("policy") != "rolling_loss_selection_v2"):
        raise ValueError("STRICT_SEVEN_MODEL_POLICY_REQUIRED")
    kickoff, end, cutoff, available = (timestamp(result[k]) for k in
                                      ("kickoff", "end", "cutoff", "assumed_available_at"))
    if not timestamp(forecast["origin"]) <= cutoff < available <= kickoff < end:
        raise ValueError("INVALID_FORECAST_ORIGIN")
    rows = result["predictions"]
    if not rows or len(rows) > 512:
        raise ValueError("INVALID_PREDICTIONS")
    hours, previous = [], -1
    for prediction in rows:
        at = timestamp(prediction["timestamp"])
        quantiles = {decimal(k, "quantile"): decimal(v, "price")
                     for k, v in prediction["quantiles"].items()}
        ordered = sorted(quantiles)
        if (at <= previous or any(not 0 < k < 1 or not 0 <= quantiles[k] <= 1 for k in ordered)
                or any(quantiles[a] > quantiles[b] for a, b in itertools.pairwise(ordered))
                or decimal(".99", "p99") not in quantiles):
            raise ValueError("INVALID_QUANTILE_CURVE_OR_TIME")
        previous = at
        if kickoff < at <= end and at % 3600 == 0 and not prediction.get("interpolated_model_point"):
            hours.append(prediction)
    return hours


def clean_points(points):
    indexed, conflicts = {}, set()
    for point in points:
        at, bid, ask = (finite(point.get(k)) for k in ("at", "bid", "ask"))
        if at is None or bid is None or ask is None or not 0 <= bid <= ask <= 1:
            continue
        value = {"at": at, "bid": bid, "ask": ask}
        if at in indexed and indexed[at] != value:
            conflicts.add(at)
        indexed[at] = value
    return [indexed[at] for at in sorted(indexed) if at not in conflicts]


def candidate_rows(row, result, points):
    """Original-side quotes; at most one next-minute observation per signal hour.

    Signal ASK must exceed P99; the next-minute ASK must still exceed it.
    Opposite ASK is 1-original BID from that same observation. No future
    observation is used for volatility, crossing detection or candidate ranking.
    """
    if row.get("side") not in SIDES.get(row.get("provider"), {}):
        raise ValueError("INVALID_ECONOMIC_SIDE")
    hours = forecast_hours(result)
    quotes = clean_points(points)
    result_rows, counts = [], Counter()
    prediction_hash = hashlib.sha256(json.dumps(result["predictions"], sort_keys=True,
                                                allow_nan=False).encode()).hexdigest()
    final_p99 = result["predictions"][-1]["quantiles"]["0.99"]
    for prediction in hours:
        hour, p99 = prediction["timestamp"], decimal(prediction["quantiles"]["0.99"], "p99")
        observed = [p for p in quotes if hour-60 < p["at"] <= hour]
        following = [p for p in quotes if hour < p["at"] <= hour+60]
        if not observed:
            counts["missing_signal_quote"] += 1
            continue
        signal = observed[-1]
        if decimal(signal["ask"], "signal_ask") <= p99:
            counts["not_above_hourly_p99"] += 1
            continue
        if not following:
            counts["missing_next_minute_quote"] += 1
            continue
        quote = following[0]
        if decimal(quote["ask"], "original_ask") <= p99:
            counts["crossing_did_not_persist"] += 1
            continue
        if row.get("market_close") and quote["at"] >= timestamp(row["market_close"]):
            counts["market_closed"] += 1
            continue
        price = 1-decimal(quote["bid"], "original_bid")
        if not decimal(".01", "min_price") <= price < 1:
            counts["unavailable_opposite_ask"] += 1
            continue
        evidence = result.get("settlement")
        if evidence and evidence.get("settlement_at") and timestamp(evidence["settlement_at"]) < quote["at"]:
            counts["already_settled"] += 1
            continue
        step = decimal(result["quantity_step"], "quantity_step")
        if step not in (decimal(".01", "step"), 1):
            raise ValueError("UNSUPPORTED_QUANTITY_RULES")
        quantity = (1/price/step).to_integral_value(rounding=ROUND_DOWN)*step
        paid_fee = fee(row["provider"], quantity, price, result["fee_coefficient"])
        # Volatility is the same prior-hour quote statistic used in the P01
        # study; its P01-specific standardized discount is not reused here.
        features = capture(quotes, hour, float(p99), signal["ask"], prediction["quantiles"])
        features.pop("discount_quote_sigma")
        purchased_side = SIDES[row["provider"]][row["side"]]
        payout = None
        if evidence:
            affirmative = decimal(evidence["affirmative_payout"], "payout")
            if not 0 <= affirmative <= 1:
                raise ValueError("INVALID_SETTLEMENT_PAYOUT")
            payout = affirmative if purchased_side in {"yes", "long"} else 1-affirmative
        result_rows.append({**features, "portfolio": P99,
            "fixture_id": row.get("fixture_id"), "identity_verified": bool(row.get("fixture_id")),
            "provider": row["provider"], "symbol": row["symbol"], "event_title": row["event_title"],
            "signal_side": row["side"], "side": purchased_side,
            "signal_contract_id": row["contract_id"], "contract_id": row["symbol"]+":"+purchased_side,
            "signal_at": hour, "signal_quote_at": signal["at"], "signal_ask": signal["ask"],
            "signal_bid": signal["bid"], "signal_bid_margin": str(decimal(signal["bid"], "signal_bid")-p99),
            "p99": str(p99), "final_p99": final_p99,
            "overshoot": str(decimal(signal["ask"], "signal_ask")-p99),
            "signal_entry_ask": quote["ask"], "opposite_ask": str(price),
            "entry_spread": str(decimal(quote["ask"], "ask")-decimal(quote["bid"], "bid")),
            "decision_at": quote["at"], "price": str(price), "quantity": str(quantity),
            "entry_cost": str(price*quantity), "entry_fee": str(paid_fee),
            "payout": None if payout is None else str(payout),
            "settlement_at": evidence.get("settlement_at") if evidence else None,
            "forecast_id": result["forecast"]["forecast_id"], "forecast_sha256": prediction_hash,
            "published_at": result["assumed_available_at"], "historical_publication": result.get("historical_publication", False),
            "accounting": "next_recorded_minute_opposite_ask_quote_proxy_no_depth"})
    return result_rows, dict(counts)


def select(rows):
    """One position per verified fixture, across both venues, without outcomes.

    Prefer the earliest decision; simultaneous candidates are ordered by known
    original-side overshoot, then stable provider/contract IDs. Overshoot is a
    ranking feature, not an estimate of expected settlement profit.
    """
    ranked = sorted((r for r in rows if r.get("identity_verified") and r.get("fixture_id")),
                    key=lambda r: (r["decision_at"], -decimal(r["overshoot"], "overshoot"),
                                   r["provider"], r["signal_contract_id"]))
    seen, output = set(), []
    for row in ranked:
        if row["fixture_id"] not in seen:
            output.append(row)
            seen.add(row["fixture_id"])
    return output


def timed_trades(rows):
    """Unknown credit times stay explicit; never invent them for DD/funding."""
    if any(r.get("payout") is not None and r.get("settlement_at") is None for r in rows):
        raise ValueError("SETTLEMENT_CREDIT_TIME_UNAVAILABLE")
    return [normalize(r, "historical_verified_p99") for r in select(rows)]
