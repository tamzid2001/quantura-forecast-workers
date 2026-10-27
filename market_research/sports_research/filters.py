"""Small fixed hypothesis family; no outcome-dependent threshold search."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass

from .data import OUTSIDE, P99


def obvious_prop(trade):
    series = trade.symbol.split("-", 1)[0].upper()
    return bool(re.search(r"EXACT|SPREAD|TOTAL|HANDICAP|PERIOD|QUARTER|FIRSTSET|SETWINNER", series)
                or re.search(r"\b(?:exact (?:match )?score|correct score|point spread|total points|"
                             r"first half|second half|first set|second set|quarter|handicap)\b",
                             trade.title, re.IGNORECASE))


@dataclass(frozen=True)
class EntryVeto:
    name: str
    price_cap: float | None = None
    spread_cap: float | None = None
    discount_cap: float | None = None
    discount_floor: float | None = None
    fee_fraction_cap: float | None = None
    exclude_obvious_props: bool = False
    quote_sigma_cap: float | None = None
    discount_sigma_floor: float | None = None
    overshoot_cap: float | None = None
    final_p99_cap: float | None = None
    require_bid_above_p99: bool = False

    def fingerprint(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()

    def decision(self, trade):
        """Missing data is distinct from a failed filter; never invent a feature."""
        tests = [("planned_price", self.price_cap, False),
                 ("entry_spread", self.spread_cap, False),
                 ("discount", self.discount_cap, False),
                 ("discount", self.discount_floor, True),
                 ("fee_fraction", self.fee_fraction_cap, False),
                 ("quote_change_std", self.quote_sigma_cap, False),
                 ("discount_quote_sigma", self.discount_sigma_floor, True),
                 ("overshoot", self.overshoot_cap, False),
                 ("final_p99", self.final_p99_cap, False)]
        applicable = [(key, bound, lower) for key, bound, lower in tests if bound is not None]
        if any(trade.features.get(key) is None for key, _, _ in applicable):
            return "missing_feature"
        if self.require_bid_above_p99:
            margin = trade.features.get("signal_bid_margin")
            if margin is None:
                return "missing_feature"
            if margin <= 0:
                return "veto"
        if ((self.quote_sigma_cap is not None or self.discount_sigma_floor is not None)
                and (trade.features.get("quote_change_pairs") or 0) < 20):
            return "missing_feature"
        if self.exclude_obvious_props and obvious_prop(trade):
            return "veto"
        for key, bound, lower in applicable:
            value = trade.features[key]
            if (value + 1e-10 < bound) if lower else (value > bound + 1e-10):
                return "veto"
        return "keep"


def candidates(strategy):
    if strategy == P99:
        return [EntryVeto("baseline"),
                *[EntryVeto(f"opposite_price_at_most_{c}c", price_cap=c/100) for c in (10, 20, 30, 50)],
                EntryVeto("spread_at_most_1c", spread_cap=.01),
                EntryVeto("spread_at_most_2c", spread_cap=.02),
                EntryVeto("original_bid_above_p99", require_bid_above_p99=True),
                EntryVeto("overshoot_at_most_5c", overshoot_cap=.05),
                EntryVeto("overshoot_at_most_10c", overshoot_cap=.10),
                EntryVeto("final_p99_at_most_50c", final_p99_cap=.50),
                EntryVeto("exclude_obvious_props", exclude_obvious_props=True),
                EntryVeto("opposite30_spread1", price_cap=.30, spread_cap=.01),
                EntryVeto("bid_confirmed_spread1_no_props", spread_cap=.01,
                          require_bid_above_p99=True, exclude_obvious_props=True)]
    band = [EntryVeto("discount_at_least_15c", discount_floor=.15),
            EntryVeto("discount_at_least_20c", discount_floor=.20)] if strategy == OUTSIDE else [
                EntryVeto("discount_at_most_2c", discount_cap=.02),
                EntryVeto("discount_at_most_5c", discount_cap=.05)]
    return [EntryVeto("baseline"),
            EntryVeto("price_at_most_50c", price_cap=.50),
            EntryVeto("price_at_most_60c", price_cap=.60),
            EntryVeto("price_at_most_70c", price_cap=.70),
            EntryVeto("spread_at_most_1c", spread_cap=.01),
            EntryVeto("spread_at_most_2c", spread_cap=.02),
            *band,
            EntryVeto("fees_at_most_2pct", fee_fraction_cap=.02),
            EntryVeto("exclude_obvious_props", exclude_obvious_props=True),
            EntryVeto("price60_spread1", price_cap=.60, spread_cap=.01),
            EntryVeto("price60_spread1_no_props", price_cap=.60, spread_cap=.01,
                      exclude_obvious_props=True),
            EntryVeto("quote_change_std_at_most_2c", quote_sigma_cap=.02),
            EntryVeto("discount_at_least_2_quote_sigma", discount_sigma_floor=2.0)]


def apply(trades, veto):
    decisions = [(t, veto.decision(t)) for t in trades]
    return [t for t, d in decisions if d == "keep"], {
        "kept": sum(d == "keep" for _, d in decisions),
        "vetoed": sum(d == "veto" for _, d in decisions),
        "missing_feature": sum(d == "missing_feature" for _, d in decisions),
        "feature_coverage": sum(d != "missing_feature" for _, d in decisions) / len(trades) if trades else None}
