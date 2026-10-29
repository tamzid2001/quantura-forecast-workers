from decimal import Decimal
import gzip
import json
from unittest.mock import patch

import pytest

from market_research import game_p01_paper as paper
from market_research.game_p01_hourly_comparison import (
    FORWARD_HOURLY_RULES, HOURLY_RULES, OUTSIDE, TIGHT, WITHIN,
    ensure_portfolios, entry_quote, fill_hourly, follow_tight, select_hourly)
from market_research.game_p01_portfolio import BASELINE, fill, performance


def candidate(**updates):
    return {"fixture_id": "game", "identity_verified": True, "seven_model_verified": True,
            "provider": "kalshi", "side": "yes", "contract_id": "original-contract", "symbol": "market",
            "forecast_id": "forecast", "event_title": "Alpha vs Beta", "event_id": "event",
            "signal_at": 3600, "signal_quote_at": 3600, "signal_ask": ".50",
            "price": ".50", "opposite_ask": ".52", "p01": ".60", "final_p01": ".70",
            "decision_at": 3660, "quote_at": 3660, "published_at": 3590,
            "game_start": 1800, "forecast_end": 10000, "market_close": 9000,
            "fee_coefficient": ".07", "quantity_step": ".01", "minimum_quantity": ".01", **updates}


def select(rows, rule, used=()):
    return select_hourly(rows, used, rule, 3600)[0]


@pytest.mark.parametrize("signal,within,outside", [(".50", 1, 0), (".4999", 0, 1), (".5001", 1, 0)])
def test_hour_band_is_mutually_exclusive_with_exact_ten_cents_inside(signal, within, outside):
    c = candidate(signal_ask=signal)
    assert len(select([c], WITHIN)) == within
    assert len(select([c], OUTSIDE)) == outside


def test_classification_uses_hourly_quote_not_next_minute_price_or_forecast_end():
    c = candidate(signal_ask=".49", price=".55", opposite_ask=".47", final_p01=".99")
    assert select([c], OUTSIDE) and not select([c], WITHIN)
    # A price falling more than 10c only after the hour stays in the within group.
    c = candidate(signal_ask=".55", price=".40", opposite_ask=".62")
    assert select([c], WITHIN) and not select([c], OUTSIDE)


@pytest.mark.parametrize("provider,side,inverse", [("kalshi", "yes", "no"), ("kalshi", "no", "yes"),
    ("polymarket_us", "long", "short"), ("polymarket_us", "short", "long")])
def test_both_venues_flip_economic_side_and_charge_spread_and_fees(provider, side, inverse):
    c = candidate(provider=provider, side=side, signal_ask=".30", price=".33", opposite_ask=".70")
    t = fill_hourly(c, OUTSIDE)
    assert t["side"] == inverse and t["signal_side"] == side
    assert Decimal(t["price"]) == Decimal(".70")  # NOT 1 minus .33.
    assert Decimal(t["quantity"]) == Decimal("1.42")
    assert Decimal(t["entry_cost"]) <= 1 and Decimal(t["entry_fee"]) > 0
    assert t["forecast_id"] != c["forecast_id"]
    assert t["original_forecast_id"] == c["forecast_id"]


@pytest.mark.parametrize("values", [{"signal_quote_at": 3601}, {"signal_quote_at": 3540},
    {"signal_ask": ".60"}, {"final_p01": ".5"}, {"published_at": 3601}, {"decision_at": 3661},
    {"seven_model_verified": False}, {"identity_verified": False}, {"opposite_ask": None},
    {"opposite_ask": ".10"}, {"quantity_step": None}, {"quantity_step": ".5"},
    {"side": "long"}, {"game_start": 3600}])
def test_invalid_or_missing_receipts_are_not_fills(values):
    assert not select([candidate(**{"signal_ask": ".4", **values})], OUTSIDE)


def test_quantity_step_and_one_lifetime_entry_per_game_across_venues_and_props():
    a = candidate(signal_ask=".4", quantity_step="1", minimum_quantity="1")
    b = candidate(signal_ask=".3", provider="polymarket_us", side="short", contract_id="other-prop")
    assert Decimal(fill_hourly(a, OUTSIDE)["quantity"]) == 1
    chosen = select([a, b], OUTSIDE)
    assert len(chosen) == 1
    assert not select([a, b], OUTSIDE, ["game"])
    # The separate within portfolio has its own game-exposure ledger.
    assert select([candidate()], WITHIN)


def test_selection_never_uses_future_outcomes():
    rows = [candidate(signal_ask=".4"), candidate(signal_ask=".3", provider="polymarket_us", side="long")]
    before = select(rows, OUTSIDE)[0]["provider"]
    rows[0]["payout"], rows[1]["payout"] = 1, 0
    assert select(rows, OUTSIDE)[0]["provider"] == before
    assert "payout" not in fill_hourly(rows[0], OUTSIDE)


def test_upgrade_old_checkpoint_keeps_history_and_starts_next_future_hour(tmp_path):
    state = paper.fresh_state(3500)
    for r in FORWARD_HOURLY_RULES:
        state["portfolios"].pop(r.name)
    original = fill(candidate())
    state["portfolios"][BASELINE.name]["trades"] = [original]
    paper.validate_state(state)
    ensure_portfolios(state, 7400)
    assert state["portfolios"][BASELINE.name]["trades"] == [original]
    for rule in FORWARD_HOURLY_RULES:
        p = state["portfolios"][rule.name]
        assert p["activation_at"] == 7400 and p["first_signal_at"] == 10800
        assert not p["trades"]
        assert select_hourly([candidate()], (), rule, p["first_signal_at"])[0] == []
    paper.save(state, tmp_path)
    restored = json.loads(gzip.decompress((tmp_path/"paper-state.json.gz").read_bytes()))
    ensure_portfolios(restored, 99999)
    assert restored["portfolios"][WITHIN.name]["activation_at"] == 7400
    paper.validate_state(restored)
    restored["portfolios"][WITHIN.name]["rules_hash"] = "different"
    with pytest.raises(ValueError, match="RULES_CHANGED"):
        paper.validate_state(restored)


def test_opposite_settlement_and_shared_quote_marks_are_separate(tmp_path):
    state = paper.fresh_state(3500)
    original = fill(candidate(signal_ask=".4"))
    opposite = fill_hourly(candidate(signal_ask=".4"), OUTSIDE)
    state["portfolios"][BASELINE.name]["trades"] = [original]
    state["portfolios"][OUTSIDE.name]["trades"] = [opposite]
    with patch.object(paper, "market", return_value={"status": "settled", "result": "yes"}):
        paper.update_settlements(state, 9500)
    assert original["payout"] == "1.0" and opposite["payout"] == "0.0"
    assert performance([opposite])["losses"] == 1
    paper.stats(state, tmp_path)
    report = json.loads((tmp_path/"comparison-stats.json").read_text())
    assert set(report["portfolios"]) == {r.name for r in FORWARD_HOURLY_RULES}
    assert report["portfolios"][OUTSIDE.name]["losses"] == 1
    assert "signal_side" in (tmp_path/"comparison-ledger.csv").read_text()


def test_identical_side_in_separate_portfolios_receives_same_available_bid():
    state = paper.fresh_state(3500)
    a = fill(candidate())
    b = fill_hourly(candidate(), WITHIN)
    state["portfolios"][BASELINE.name]["trades"] = [a]
    state["portfolios"][WITHIN.name]["trades"] = [b]
    with patch.object(paper, "market", return_value={"status": "active"}), \
            patch.object(paper, "prices", return_value=[{"at": 4000, "bid": .4, "ask": .5}]) as lookup:
        paper.update_settlements(state, 4000)
    assert lookup.call_count == 1
    for name in (BASELINE.name, WITHIN.name):
        assert state["portfolios"][name]["equity"]["unmarked_positions"] == 0
        assert state["portfolios"][name]["equity"]["sampled_pnl_usd"] < 0


def test_worker_checkpoints_both_portfolios_and_retry_does_not_repeat_game(tmp_path):
    state = paper.fresh_state(3500)
    candidates = [candidate(fixture_id="within"), candidate(fixture_id="outside", signal_ask=".4")]
    with patch.object(paper, "restore", return_value=state), patch.object(paper, "artifacts", return_value=[]), \
            patch.object(paper, "snapshots_at", return_value=([], {})), \
            patch.object(paper, "candidates_at", return_value=(candidates, {})), \
            patch.object(paper, "update_settlements", return_value={}):
        paper.run(tmp_path, now=3800)
        paper.run(tmp_path, now=3800)
    saved = json.loads(gzip.decompress((tmp_path/"paper-state.json.gz").read_bytes()))
    assert len(saved["portfolios"][WITHIN.name]["trades"]) == 1
    assert len(saved["portfolios"][OUTSIDE.name]["trades"]) == 1
    assert saved["portfolios"][OUTSIDE.name]["trades"][0]["side"] == "no"
    assert saved["next_hour"] == 7200
    report = json.loads((tmp_path/"comparison-stats.json").read_text())
    assert report["live_orders"] == 0 and report["portfolios"][WITHIN.name]["entries"] == 1
    summary = (tmp_path/"comparison-summary.md").read_text()
    assert "Outside 10c: opposite side" in summary and "N/A" in summary
    assert "Retrospective results were not imported" in summary


def test_provider_candidate_retains_signal_ask_and_spread_aware_opposite_quote():
    row = {"id": "f", "provider": "polymarket_us", "symbol": "game", "contract_id": "long", "side": "long",
           "event_id": "p", "event_title": "Alpha vs Beta", "game_start": paper.iso(1800),
           "forecast_end": paper.iso(10000), "predictions": [{"timestamp": paper.iso(3600), "quantiles": {"0.01": .6}}],
           "artifact_id": 1, "publication_ts": 3500}
    other = {**row, "provider": "kalshi", "event_id": "k", "predictions": []}
    samples = [{"at": 3600, "ask": .3, "bid": .28}, {"at": 3660, "ask": .4, "bid": .37}]
    with patch.object(paper, "prices", return_value=samples), \
            patch.object(paper, "market", return_value={"endDate": paper.iso(9000), "feeCoefficient": ".0695", "minimumTradeQty": ".01"}):
        candidates, _ = paper.candidates_at([row, other], 3600, {})
    c = candidates[0]
    assert c["signal_ask"] == .3 and c["signal_quote_at"] == 3600
    assert Decimal(c["opposite_ask"]) == Decimal(".63")
    assert fill_hourly(c, OUTSIDE)["side"] == "short"


@pytest.mark.parametrize("hour_ask,allowed", [(".58", True), (".580001", True),
    (".579999", False), (".60", False), (".61", False)])
def test_tight_band_exact_boundary_and_original_side(hour_ask, allowed):
    c = candidate(signal_ask=hour_ask, price=".57")
    chosen = select([c], TIGHT)
    assert bool(chosen) == allowed
    if allowed:
        trade = fill_hourly(chosen[0], TIGHT)
        assert trade["side"] == c["side"] and Decimal(trade["price"]) == Decimal(".57")


def test_tight_uses_hourly_discount_even_when_next_minute_moves():
    assert select([candidate(signal_ask=".59", price=".40")], TIGHT)
    assert not select([candidate(signal_ask=".55", price=".59")], TIGHT)


def test_tight_vetoes_parent_first_choice_without_alternative_or_later_reentry(tmp_path):
    state = paper.fresh_state(3500)
    # Parent ranks its larger known edge first, even though the other venue's
    # proposition would pass 2c. Neither the alternative nor a later hour replaces it.
    first = [candidate(signal_ask=".54", price=".54"),
             candidate(signal_ask=".59", price=".59", provider="polymarket_us", side="long",
                       contract_id="alternate", forecast_id="alternate")]
    later = [candidate(signal_ask=".59", price=".59", signal_at=7200,
                       signal_quote_at=7200, decision_at=7260, quote_at=7260)]
    with patch.object(paper, "restore", return_value=state), patch.object(paper, "artifacts", return_value=[]), \
            patch.object(paper, "snapshots_at", return_value=([], {})), \
            patch.object(paper, "candidates_at", side_effect=[(first, {}), (later, {})]), \
            patch.object(paper, "update_settlements", return_value={}):
        paper.run(tmp_path, now=7400)
    parent, tight = (state["portfolios"][r.name] for r in (WITHIN, TIGHT))
    assert len(parent["trades"]) == 1 and parent["trades"][0]["provider"] == "kalshi"
    assert not tight["trades"]
    assert tight["coverage"]["3600"]["parent_first_choices"] == 1
    assert tight["coverage"]["7200"]["parent_first_choices"] == 0


def test_tight_accepts_parent_receipt_checkpoints_and_settles_same_side(tmp_path):
    state = paper.fresh_state(3500)
    c = candidate(signal_ask=".59", price=".57")
    with patch.object(paper, "restore", return_value=state), patch.object(paper, "artifacts", return_value=[]), \
            patch.object(paper, "snapshots_at", return_value=([], {})), \
            patch.object(paper, "candidates_at", return_value=([c], {})), \
            patch.object(paper, "update_settlements", return_value={}):
        paper.run(tmp_path, now=3800)
        paper.run(tmp_path, now=3800)
    tight = state["portfolios"][TIGHT.name]
    assert len(tight["trades"]) == 1
    paper.validate_state(state)
    with patch.object(paper, "market", return_value={"status": "settled", "result": "yes"}):
        paper.update_settlements(state, 9500)
    assert tight["trades"][0]["payout"] == "1.0"
    paper.stats(state, tmp_path)
    stats = json.loads((tmp_path/"comparison-stats.json").read_text())["portfolios"][TIGHT.name]
    assert stats["wins"] == 1 and stats["by_provider"]["kalshi"]["wins"] == 1
    assert TIGHT.name in (tmp_path/"comparison-ledger.csv").read_text()
    assert "Within 2c: original first choice" in (tmp_path/"comparison-summary.md").read_text()


def test_tight_activation_never_imports_old_parent_trades():
    state = paper.fresh_state(3500)
    state["portfolios"].pop(TIGHT.name)
    old = candidate(signal_ask=".59")
    state["portfolios"][WITHIN.name]["trades"] = [fill_hourly(old, WITHIN)]
    ensure_portfolios(state, 7400)
    tight = state["portfolios"][TIGHT.name]
    follow_tight([old], tight, 3600)
    assert tight["first_signal_at"] == 10800 and not tight["trades"]
    assert not tight["coverage"]
    ensure_portfolios(state, 99999)
    assert tight["activation_at"] == 7400


def test_tight_checkpoint_rejects_unmatched_parent_or_altered_entry():
    state = paper.fresh_state(3500)
    c = candidate(signal_ask=".59")
    tight = state["portfolios"][TIGHT.name]
    follow_tight([c], tight, 3600)
    with pytest.raises(ValueError, match="INVALID_TIGHT_PARENT"):
        paper.validate_state(state)
    state["portfolios"][WITHIN.name]["trades"] = [fill_hourly(c, WITHIN)]
    paper.validate_state(state)
    tight["trades"][0]["side"] = "no"
    with pytest.raises(ValueError, match="INVALID_TIGHT_PARENT"):
        paper.validate_state(state)


def test_historical_campaign_keeps_original_rule_set():
    assert HOURLY_RULES == (WITHIN, OUTSIDE)
    assert TIGHT not in HOURLY_RULES
