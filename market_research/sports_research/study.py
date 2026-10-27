"""Exploratory chronological comparisons, with explicitly purged label timing."""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone

from . import VERSION
from .data import day, iso
from .filters import apply, candidates, obvious_prop
from .statistics import (
    blocks,
    bonferroni,
    bootstrap_comparison,
    daily,
    independent_sample_planning,
    sign_p_value,
    summary,
)


def split(trades, cutoff):
    # A label learned after the selection cutoff cannot enter training, even
    # when the original position opened earlier. No game moves between splits.
    train = [t for t in trades if t.entry_at < cutoff and t.closed and t.settled_at < cutoff]
    validation = [t for t in trades if t.entry_at >= cutoff]
    excluded = [t for t in trades if t.entry_at < cutoff and t not in train]
    if {t.fixture for t in train} & {t.fixture for t in validation}:
        raise ValueError("GAME_LEAKAGE_ACROSS_SPLITS")
    return train, validation, excluded


def cutoff_for(trades, historical=False):
    if historical:
        # Frozen 70/30 calendar split of the requested September 20–October 19 study.
        return datetime(2026, 10, 11, tzinfo=timezone.utc).timestamp()
    dates = sorted({day(t.entry_at) for t in trades})
    if not dates:
        return 0
    first, last = date.fromisoformat(dates[0]), date.fromisoformat(dates[-1])
    length = (last - first).days + 1
    cutoff_day = first + timedelta(days=max(1, int(length * .7)))
    return datetime.combine(cutoff_day, time(), timezone.utc).timestamp()


def paired_sign(baseline, selected):
    b, s = daily(baseline), daily(selected)
    changes = [s.get(d, {}).get("profit", 0.0) - v["profit"] for d, v in b.items()]
    return sign_p_value(blocks(changes))


def comparison(baseline, veto, draws=2000, seed=20261027):
    selected, coverage = apply(baseline, veto)
    stats = summary(selected)
    uncertainty = bootstrap_comparison(baseline, selected, draws, seed)
    return {"filter": veto.name, "filter_sha256": veto.fingerprint(),
            "status": "not_evaluable" if not selected else "exploratory",
            "coverage": coverage, "statistics": stats, "uncertainty": uncertainty,
            "paired_block_sign_p_improvement": paired_sign(baseline, selected),
            "stress_roi_pct": {"extra_1c_per_contract": summary(selected, .01)["roi_pct"],
                               "extra_2c_per_contract": summary(selected, .02)["roi_pct"]}}


def diagnostic_cuts(trades):
    groups = defaultdict(list)
    for t in trades:
        price = float(t.planned_price)
        bucket = "under_30c" if price < .3 else "30_to_50c" if price < .5 else "50_to_70c" if price < .7 else "70c_and_above"
        groups["price:" + bucket].append(t)
        groups["provider:" + t.provider].append(t)
        groups["contract:obvious_prop" if obvious_prop(t) else "contract:not_obvious_prop_or_unclassified"].append(t)
    return {name: summary(rows) for name, rows in sorted(groups.items())}


def training_choice(comparisons):
    eligible = []
    for item in comparisons:
        s, u, c = item["statistics"], item["uncertainty"], item["coverage"]
        if (s["settled"] >= 30 and s["trading_days"] >= 8 and s["open"] == 0
                and c["feature_coverage"] is not None and c["feature_coverage"] >= .9
                and u.get("roi_ci95_pct") is not None):
            eligible.append(item)
    if not eligible:
        return None
    # This is ONLY a rule to choose what to test prospectively, not proof of an edge.
    return min(eligible, key=lambda x: (-x["uncertainty"]["roi_ci95_pct"][0],
                                         -x["statistics"]["closed_spend_usd"], x["filter"]))["filter"]


def analyze(trades, sources, draws=2000, seed=20261027):
    groups = defaultdict(list)
    for t in trades:
        groups[t.cohort + ":" + t.strategy].append(t)
    output = {"schema": VERSION, "sources": sources, "cohorts": {},
              "live_orders": 0, "automatic_live_promotion": False,
              "inference_scope": "exploratory observed cohorts; historical reports already inspected; no untouched holdout claim",
              "candidate_semantics": "entry vetoes on the baseline's first recorded game choice; no invented fills or replacement entries",
              "price_quantile_is_settlement_probability": False}
    for key, rows in sorted(groups.items()):
        rows = sorted(rows, key=lambda t: (t.entry_at, t.fixture))
        cutoff = cutoff_for(rows, rows[0].cohort.startswith("historical_verified"))
        train, validation, purged = split(rows, cutoff)
        vetoes = candidates(rows[0].strategy)
        stages = {label: [comparison(data, v, draws, seed) for v in vetoes]
                  for label, data in [("all_observed", rows), ("training", train), ("validation", validation)]}
        choice = training_choice(stages["training"])
        base_stats = summary(rows)
        sigma = base_stats["std_net_per_game_usd"]
        planning = independent_sample_planning(sigma) if sigma else None
        output["cohorts"][key] = {"baseline": base_stats, "iid_sample_size_sensitivity_for_5c_gain": planning, "cuts": diagnostic_cuts(rows),
                                  "cutoff_utc": iso(cutoff), "purged_training_labels": len(purged),
                                  "training_selected_filter": choice,
                                  "selection_basis": "training lower pointwise block-bootstrap ROI bound; tie by spend then stable filter ID",
                                  "stages": stages}
    # Correct the declared finite family, including underpowered/missing-feature
    # candidates and all three reported stages. Repeated/overlapping tests still count.
    family = max(1, 3 * 2 * sum(len(candidates(rows[0].strategy)) for rows in groups.values()))
    for cohort in output["cohorts"].values():
        for items in cohort["stages"].values():
            for item in items:
                item["multiple_test_family"] = family
                item["block_sign_p_positive_median_bonferroni"] = bonferroni(
                    item["statistics"]["block_sign_p_positive_median"], family)
                item["paired_block_sign_p_improvement_bonferroni"] = bonferroni(
                    item["paired_block_sign_p_improvement"], family)
    output["configuration_sha256"] = hashlib.sha256(json.dumps(
        {"version": VERSION, "seed": seed, "draws": draws,
         "families": {s: [v.fingerprint() for v in candidates(s)] for s in sorted({t.strategy for t in trades})}},
        sort_keys=True).encode()).hexdigest()
    return output


def markdown(report):
    def number(v, digits=2):
        return "N/A" if v is None else f"{v:,.{digits}f}"
    lines = ["# Sports strategy statistical research", "", report["inference_scope"], "",
             "All P&L includes recorded fees. Cohorts and execution types are kept separate. P01 is a market-price quantile, not an event win probability.", "",
             "| Cohort / strategy | Settled | Net USD | ROI % | Std / game USD | Variance USD² | Trading days |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for name, c in report["cohorts"].items():
        s = c["baseline"]
        lines.append(f"| {name} | {s['settled']} | {number(s['net_pnl_usd'])} | {number(s['roi_pct'])} | {number(s['std_net_per_game_usd'], 4)} | {number(s['variance_net_per_game_usd2'], 4)} | {s['trading_days']} |")
    for name, c in report["cohorts"].items():
        lines.extend(["", "## " + name, "",
                      f"Exploratory selection cutoff: {c['cutoff_utc']}. Purged late/unsettled training labels: {c['purged_training_labels']}. Training-only choice for future shadow testing: **{c['training_selected_filter'] or 'insufficient evidence'}**.", "",
                      "| Filter | Training N | Training ROI % | Validation N | Validation ROI % | Validation ROI 95% interval | Adjusted improvement sign p | Missing validation features |",
                      "|---|---:|---:|---:|---:|---|---:|---:|"])
        for train, val in zip(c["stages"]["training"], c["stages"]["validation"]):
            ci = val["uncertainty"].get("roi_ci95_pct")
            rendered = "N/A" if ci is None else f"[{number(ci[0])}, {number(ci[1])}]"
            lines.append(f"| {train['filter']} | {train['statistics']['settled']} | {number(train['statistics']['roi_pct'])} | {val['statistics']['settled']} | {number(val['statistics']['roi_pct'])} | {rendered} | {number(val['paired_block_sign_p_improvement_bonferroni'], 4)} | {val['coverage']['missing_feature']} |")
    lines.extend(["", "## Interpretation and limits", "",
                  "- The three-trading-day block bootstrap retains same-day observations together. Its percentile intervals remain pointwise exploratory estimates and assume the observed cohort is sufficiently representative and locally stable.",
                  "- The exact sign p-value concerns the median of independent nonzero three-trading-day profit blocks, not the mean return or the probability a strategy works. Bonferroni correction covers the declared finite candidate family; it does not turn post-hoc exploration into a preregistered experiment.",
                  "- Missing volatility/disagreement evidence is reported as missing. Entry-quote Brier/log loss are baselines only; selected trades cannot establish unconditional forecast-quantile calibration.",
                  "- Additional 1c/2c execution cost is modeled per filled contract with recorded quantities and fees held fixed. It does not reproduce a new book or fill probability.",
                  "- Partial historical coverage, open positions and different execution models prevent a claim of demonstrated live profitability. Inspect JSON for source receipts, open counts, risk, calibration baselines, cuts and stress results.",
                  "- Automated daily reporting never sends orders. Any frozen prospective trial is evaluated at its fixed deadline; repeated daily looks cannot trigger live promotion.", ""])
    return "\n".join(lines)
