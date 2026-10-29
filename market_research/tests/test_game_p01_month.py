from unittest.mock import patch
import json
import time

import pytest

from market_research import game_p01_month as study
from market_research.game_p01_hourly_comparison import WITHIN, OUTSIDE, fill_hourly, select_hourly
from market_research.tests.test_game_p01_hourly_comparison import candidate


def test_date_window_is_thirty_completed_new_york_days():
    a, b = study.bounds("2026-09-20", "2026-10-19")
    assert study.iso(a) == "2026-09-20T04:00:00Z"
    assert study.iso(b) == "2026-10-20T04:00:00Z"
    assert b-a == 30*86400


@pytest.mark.parametrize("first,last", [("2026-10-20", "2026-09-20"), ("2025-01-01", "2026-10-19")])
def test_bad_date_ranges_fail(first, last):
    with pytest.raises(ValueError):
        study.bounds(first, last)


def test_stops_before_start_hour_even_when_start_is_half_past():
    assert study.pregame_cutoff(17*3600+1800) == 16*3600
    assert study.pregame_cutoff(17*3600) == 16*3600


@pytest.mark.parametrize("ticker,accept", [("KXNFLGAME", True), ("KXATPMATCH", True),
    ("KXNFL1HGAME", False), ("KXNFLTOTAL", False), ("KXFIFAOPPONENTGAME", False)])
def test_only_game_series(ticker, accept):
    assert study.game_series(ticker) == accept


def test_kalshi_schedule_never_uses_expiration_or_occurrence_time():
    event = {"event_ticker": "E", "title": "A vs B", "markets": []}
    assert study.kalshi_rows(event, [], {"ticker": "KXGAME"}, 0, 99999)[1] == "missing_or_conflicting_linked_milestone"
    unrelated = [{"start_date": study.iso(3600), "primary_event_tickers": ["OTHER"]}]
    assert not study.kalshi_rows(event, unrelated, {"ticker": "KXGAME"}, 0, 99999)[0]
    conflicting = [{"start_date": study.iso(t), "primary_event_tickers": ["E"]} for t in (3600, 7200)]
    assert not study.kalshi_rows(event, conflicting, {"ticker": "KXGAME"}, 0, 99999)[0]


def test_poly_filters_each_market_and_does_not_select_on_resolution():
    market = {"slug": "game", "marketType": "moneyline", "gameStartTime": study.iso(50000),
              "createdAt": study.iso(1000), "endDate": study.iso(60000), "status": "MARKET_STATUS_OPEN",
              "marketSides": [{"id": "L", "long": True}, {"id": "S", "long": False}]}
    event = {"id": 1, "title": "A vs B", "markets": [market, {**market, "gameStartTime": study.iso(100000)}]}
    rows, skips = study.poly_rows(event, 0, 99999)
    assert len(rows) == 2 and {r["side"] for r in rows} == {"long", "short"}
    assert skips["outside_dates"] == 1


def test_opposite_ask_retains_spread_and_completed_hour_drops_future_ticks():
    raw = {"history": [{"timestamp": 3599, "longPrice": .4, "shortPrice": .65},
                       {"timestamp": 3600, "longPrice": .9, "shortPrice": .2},
                       {"timestamp": 7201, "longPrice": .2, "shortPrice": .85}]}
    points = study.parse_points(raw, "polymarket_us", 0, 10000)
    hourly = study.completed_hours(points, 3600, "polymarket_us")
    assert len(hourly) == 1 and hourly[0]["ask"] == .4
    inverse = study.side_points(hourly, "short")
    assert inverse[0]["ask"] == .65  # NOT 1 minus original ask (.60).
    assert inverse[0]["bid"] == .6


def test_kalshi_candles_end_stamped_and_never_fill_missing_hours():
    rows = [{"at": 3600, "ask": .6, "bid": .5}, {"at": 10800, "ask": .7, "bid": .6}]
    assert [p["at"] for p in study.completed_hours(rows, 10800, "kalshi")] == [3600, 10800]


def test_native_dollar_candles_and_legacy_cents_have_same_units():
    raw = {"candlesticks": [
        {"end_period_ts": 3600, "yes_bid": {"close_dollars": ".5000"}, "yes_ask": {"close_dollars": ".6000"}},
        {"end_period_ts": 7200, "yes_bid": {"close": 50}, "yes_ask": {"close": 60}}]}
    points = study.parse_points(raw, "kalshi", 0, 10000)
    assert len(points) == 2 and all(p["ask"] == .6 and p["bid"] == .5 for p in points)


def test_checkpoint_atomic_roundtrip_and_schema(tmp_path):
    path = tmp_path/"state.gz"
    study.save(path, {"schema": study.SCHEMA, "items": {"x": "complete"}})
    assert study.load(path)["items"] == {"x": "complete"}
    assert not list(tmp_path.glob("*.tmp"))
    study.save(path, {"schema": "wrong"})
    with pytest.raises(ValueError):
        study.load(path)


def test_missing_quantity_and_special_fee_schedules_are_excluded():
    with pytest.raises(ValueError, match="MISSING_QUANTITY"):
        study.quantity_fees({"provider": "polymarket_us", "metadata": {}})
    with pytest.raises(ValueError, match="UNSUPPORTED_FEE"):
        study.quantity_fees({"provider": "kalshi", "metadata": {}, "series_fee": {"fee_type": "other"}})
    row = {"provider": "kalshi", "metadata": {}, "series_fee": {"fee_type": "quadratic", "fee_multiplier": 1}}
    assert study.quantity_fees(row) == ("1", ".07")
    row["metadata"]["fractional_trading_enabled"] = True
    assert study.quantity_fees(row) == (".01", ".07")


def test_official_partial_settlements_supported_and_conflicts_rejected():
    row = {"provider": "kalshi", "metadata": {"status": "settled", "settlement_value_dollars": ".5"}}
    assert study.settlement(row)["affirmative_payout"] == .5
    row["metadata"]["result"] = "yes"
    with pytest.raises(ValueError, match="CONFLICTING_SETTLEMENT"):
        study.settlement(row)


def test_poly_does_not_infer_outcome_from_last_quote_and_never_invents_credit_time():
    row = {"provider": "polymarket_us", "symbol": "m", "metadata": {"status": "MARKET_STATUS_OPEN"}}
    assert study.settlement(row) is None
    row["metadata"]["status"] = "MARKET_STATUS_RESOLVED"
    with patch.object(study, "public_json", return_value={"slug": "m", "settlement": 1}):
        assert study.settlement(row)["settlement_at"] is None
    with patch.object(study, "public_json", return_value={"slug": "wrong", "settlement": 1}):
        with pytest.raises(ValueError):
            study.settlement(row)


def test_known_payout_stats_do_not_claim_unknown_portfolio_drawdown_or_funding():
    trade = fill_hourly(candidate(provider="polymarket_us", side="long"), WITHIN)
    trade.update(payout=1, settlement_at=None)
    stats = study.summarize([trade])
    assert stats["wins"] == 1 and stats["net_pnl_usd"] > 0
    assert stats["recorded_minimum_cash_usd"] is None
    assert stats["max_realized_drawdown_usd"] is None
    assert stats["sampled_equity_drawdown_usd"] is None
    assert stats["fund_all_entries_without_reusing_payouts_usd"] > 1


def test_two_strategies_use_same_original_hour_and_never_future_outcomes():
    c = candidate(signal_ask=".4", price=".45", opposite_ask=".58")
    selected, _ = select_hourly([c], (), OUTSIDE, 0)
    assert len(selected) == 1
    changed = {**c, "outcome_evidence": {"affirmative_payout": 0}}
    selected2, _ = select_hourly([changed], (), OUTSIDE, 0)
    assert selected2[0]["forecast_id"] == selected[0]["forecast_id"]
    assert not select_hourly([c], (), WITHIN, 0)[0]


def test_incomplete_catalog_is_reported_without_inventing_shard_completion(tmp_path):
    study.save(tmp_path/"catalog.json.gz", {"schema": study.SCHEMA, "first": "2026-09-20", "last": "2026-10-19",
        "start": 0, "end": 999999, "done": False, "contracts": {}, "counts": {}, "poly_offset": 0, "kalshi_index": 0})
    result = study.report(tmp_path)
    assert not result["done"]
    assert all(v["entries"] == 0 for portfolios in result["portfolios"].values() for v in portfolios.values())
    assert json.loads((tmp_path/"report.json").read_text())["historical_publication"] is False


def test_reconstruct_uses_latest_500_completed_hours_and_strict_production_policy(tmp_path):
    kickoff, cutoff = 700*3600+1800, 699*3600
    row = {"provider": "kalshi", "side": "yes", "created_at": 3600, "cutoff": cutoff,
           "game_start": study.iso(kickoff), "metadata": {}, "series_fee": {"fee_type": "quadratic"}}
    raw = [{"at": i*3600, "ask": .7, "bid": .6} for i in range(2, 702)]
    def forecast(window, horizon, **kwargs):
        assert len(window) == 500
        assert window[0].timestamp == 200*3600 and window[-1].timestamp == cutoff
        assert kwargs == {"models": study.MODELS, "quantiles": study.QUANTILES, "frequency": "1h", "weight_mode": "auto", "failure_policy": "fail"}
        return {"origin": cutoff, "rows": [{"timestamp": cutoff+i*3600, "quantiles": {str(q): .7 for q in study.QUANTILES}} for i in range(1, horizon+1)],
                "models": [{"id": m, "status": "completed"} for m in study.MODELS], "weighting": {"policy": "rolling_loss_selection_v2"}}
    with patch.object(study, "cached_history", return_value=raw), \
            patch("market_research.pregame_screener.game_models", return_value=study.MODELS), \
            patch("market_research.forecast.forecast_window", side_effect=forecast):
        result = study.reconstruct(row, tmp_path, time.monotonic()+100)
    assert result["hourly_download_count"] == 698
    assert result["context_count"] == 500 and result["historical_publication"] is False
    assert result["predictions"][-1]["timestamp"] == kickoff+4*3600
    assert result["assumed_available_at"] == 700*3600


def test_candidate_hour_never_uses_later_quote_as_the_signal(tmp_path):
    row = {"provider": "kalshi", "symbol": "m", "side": "yes", "event_id": "e", "event_title": "A vs B",
           "contract_id": "m:yes", "fixture_id": "f", "market_close": 99999}
    result = {"kickoff": 1800, "end": 7200, "assumed_available_at": 1700, "forecast": {"forecast_id": "forecast"},
              "predictions": [{"timestamp": 3600, "quantiles": {"0.01": .6}}, {"timestamp": 7200, "quantiles": {"0.01": .7}}],
              "quantity_step": ".01", "fee_coefficient": ".07", "settlement": None}
    # The next-minute quote is below P01, but the actual completed-hour quote is not.
    points = [{"at": 3600, "ask": .65, "bid": .6}, {"at": 3660, "ask": .4, "bid": .3}]
    with patch.object(study, "cached_history", return_value=points):
        rows, coverage = study.candidate_rows(row, result, tmp_path, time.monotonic()+30)
    assert not rows and coverage["no_below_p01_hour_quote"] == 2
