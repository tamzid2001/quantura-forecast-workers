"""Frozen, finite-family shadow experiment. No daily re-selection or live orders."""
from __future__ import annotations

import hashlib
import json

from . import VERSION
from .data import LIMIT, OUTSIDE, WITHIN, iso, timestamp
from .filters import candidates
from .statistics import bonferroni
from .study import comparison

FAMILIES = (("paper", WITHIN), ("paper", OUTSIDE), ("limit", LIMIT))
HORIZON_DAYS = 60
MIN_TRADES = 100
MIN_DAYS = 20


def design():
    value = {"schema": VERSION, "days": HORIZON_DAYS, "minimum_trades": MIN_TRADES,
             "minimum_trading_days": MIN_DAYS, "settlement_grace_days": 2,
             "alpha": .05, "block_length_trading_days": 3,
             "sign_test_tail": "discard incomplete final three-day block",
             "candidate_semantics": "baseline first-choice veto; no replacement trade",
             "families": {cohort + ":" + strategy: {f.name: f.fingerprint() for f in candidates(strategy)}
                          for cohort, strategy in FAMILIES},
             "promotion": "manual review only; no automatic live activation"}
    value["sha256"] = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    return value


def initialize(now):
    start = (int(now) // 86400 + 1) * 86400
    return {"schema": VERSION, "created_at": iso(now), "design": design(),
            "starts_at": iso(start), "ends_at": iso(start + HORIZON_DAYS * 86400),
            "evaluate_after": iso(start + (HORIZON_DAYS + 2) * 86400)}


def validate(state):
    if state.get("schema") != VERSION or state.get("design") != design():
        raise ValueError("FROZEN_RESEARCH_DESIGN_CHANGED_REQUIRES_NEW_EXPERIMENT")
    created, start, end, final = (timestamp(state[k]) for k in (
        "created_at", "starts_at", "ends_at", "evaluate_after"))
    if (start != (int(created) // 86400 + 1) * 86400 or end - start != HORIZON_DAYS * 86400
            or final - end != 2 * 86400):
        raise ValueError("INVALID_FROZEN_RESEARCH_WINDOW")
    return state


def restore(state, now):
    """Amend the initial registration only BEFORE its first observation day.

    The sole recognized legacy design predates complete-block sign tests.
    Keep the original start/end/grace and record its hash; never silently move
    an experiment or reinterpret collected outcomes after it has started.
    """
    if state.get("design") != design():
        legacy = design()
        legacy.pop("sign_test_tail")
        legacy.pop("sha256")
        legacy["sha256"] = hashlib.sha256(json.dumps(legacy, sort_keys=True).encode()).hexdigest()
        if state.get("design") == legacy and timestamp(state["created_at"]) <= now < timestamp(state["starts_at"]):
            state = {**state, "design": design(), "amendments": [*state.get("amendments", []), {
                "at": iso(now), "previous_design_sha256": legacy["sha256"],
                "reason": "Exclude incomplete sign-test blocks before any prospective observation; dates preserved."}]}
    return validate(state)


def assess(state, trades, sources, now, draws=2000, seed=20261027):
    validate(state)
    if now < timestamp(state["created_at"]):
        raise ValueError("RESEARCH_CLOCK_PRECEDES_REGISTRATION")
    if any(t.entry_at > now or (t.closed and t.settled_at > now) for t in trades):
        raise ValueError("FUTURE_DATA_IN_RESEARCH")
    start, end = (timestamp(state[k]) for k in ("starts_at", "ends_at"))
    final = timestamp(state["evaluate_after"])
    tests = 2 * sum(len(candidates(strategy)) - 1 for _, strategy in FAMILIES)
    result = {"registration": state, "as_of": iso(now), "test_family": tests,
              "automatic_live_activation": False, "families": {}}
    for cohort, strategy in FAMILIES:
        baseline = [t for t in trades if t.cohort == cohort and t.strategy == strategy and start <= t.entry_at < end]
        source = sources.get(cohort, {})
        observed = source.get("next_hour") if cohort == "paper" else source.get("as_of")
        horizon_complete = observed is not None and timestamp(observed) >= end
        family = []
        for veto in candidates(strategy):
            measured = comparison(baseline, veto, draws, seed)
            # Daily looks are descriptive. Inference/review is deferred until
            # the fixed deadline, including a settlement grace period.
            if now < final:
                measured["decision"] = "collecting_until_fixed_deadline"
                measured["prospective_adjusted_p"] = None
                measured["prospective_adjusted_improvement_p"] = None
            else:
                s, u, c = measured["statistics"], measured["uncertainty"], measured["coverage"]
                p = bonferroni(s["block_sign_p_positive_median"], tests)
                p2 = bonferroni(measured["paired_block_sign_p_improvement"], tests)
                ci, delta = u.get("roi_ci95_pct"), u.get("mean_daily_improvement_ci95_usd")
                deficits = []
                if not horizon_complete:
                    deficits.append("SOURCE_NOT_CAUGHT_UP_THROUGH_END")
                if any(not t.closed for t in baseline):
                    deficits.append("UNSETTLED_BASELINE_GAMES")
                if s["settled"] < MIN_TRADES or s["trading_days"] < MIN_DAYS:
                    deficits.append("INSUFFICIENT_GAMES_OR_DAYS")
                if c["feature_coverage"] is None or c["feature_coverage"] < .9:
                    deficits.append("INSUFFICIENT_AT_ENTRY_FEATURES")
                if u.get("status") != "estimated" or ci is None or ci[0] <= 0:
                    deficits.append("POSITIVE_RETURN_NOT_ESTABLISHED")
                if delta is None or delta[0] <= 0:
                    deficits.append("POSITIVE_PAIRED_IMPROVEMENT_NOT_ESTABLISHED")
                if p is None or p2 is None or p > .05 or p2 > .05:
                    deficits.append("MULTIPLE_TEST_GATE_NOT_MET")
                measured.update(decision="eligible_for_manual_review" if not deficits else "not_established",
                                deficits=deficits, prospective_adjusted_p=p,
                                prospective_adjusted_improvement_p=p2)
            if veto.name == "baseline":
                measured["decision"] = "reference_only"
            family.append(measured)
        result["families"][cohort + ":" + strategy] = family
    return result
