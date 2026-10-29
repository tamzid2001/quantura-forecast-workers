import gzip
import io
import json
import zipfile
from unittest.mock import patch

import pytest

from market_research import game_p01_paper as paper
from market_research.game_p01_portfolio import BASELINE, SHALLOW, eligible, fee, fill, performance, sample_equity, select


def candidate(**values):
    return {"fixture_id": "same-game", "identity_verified": True, "seven_model_verified": True,
            "forecast_id": "forecast", "contract_id": "yes", "provider": "kalshi", "price": .4,
            "p01": .6, "final_p01": .65, "decision_at": 3660, "signal_at": 3600,
            "quote_at": 3660, "published_at": 3500, "game_start": 1800,
            "forecast_end": 10000, "market_close": 9000, **values}


def test_game_lifetime_dedup_includes_alternate_side_and_venue():
    a = candidate()
    b = candidate(provider="polymarket_us", contract_id="no", price=.3)
    c = candidate(contract_id="other-prop", signal_at=7200, decision_at=7260, quote_at=7260, price=.1)
    chosen = select([a, b, c])
    assert len(chosen) == 1 and chosen[0]["contract_id"] == "no"
    assert select([a, b, c], ["same-game"]) == []
    assert select([{**a, "fixture_id": "different-game"}, b])


@pytest.mark.parametrize("values", [{"published_at": 3601}, {"published_at": 3600.1}, {"decision_at": 3661}, {"quote_at": 3599},
    {"price": .6}, {"final_p01": .5}, {"identity_verified": False}, {"seven_model_verified": False},
    {"market_close": 3660}, {"game_start": 3600}, {"price": float("nan")}])
def test_ineligible_point_in_time_or_identity(values):
    assert not eligible(candidate(**values))


def test_candidate_does_not_read_future_outcome_and_shallow_cap_is_explicit():
    a, b = candidate(), candidate(provider="polymarket_us", price=.3)
    before = select([a, b])
    a["payout"], b["payout"] = 1, 0
    assert select([a, b])[0]["provider"] == before[0]["provider"]
    assert not eligible(candidate(), SHALLOW)
    assert eligible(candidate(price=.5), SHALLOW)
    assert eligible(candidate(p01=1))


def test_cash_fees_drawdown_and_streaks():
    trades = []
    for i, payout in enumerate([1, 1, 0, 0, 1]):
        t = fill(candidate(fixture_id=str(i), decision_at=3660+i*10, quote_at=3660+i*10, signal_at=3600+i*10))
        t.update(payout=payout, settlement_at=8000+i)
        trades.append(t)
    s = performance(trades)
    assert (s["wins"], s["losses"], s["max_win_streak"], s["max_loss_streak"]) == (3, 2, 2, 2)
    assert s["net_pnl_usd"] == pytest.approx(2.25)
    assert s["historical_minimum_cash_usd"] == pytest.approx(5.25)
    assert s["max_realized_drawdown_usd"] == pytest.approx(2.10)
    with pytest.raises(ValueError, match="DUPLICATE_GAME"):
        performance([trades[0], trades[0]])


@pytest.mark.parametrize("coefficient", [-1, float("nan"), 2])
def test_bad_provider_fee_cannot_create_negative_costs(coefficient):
    with pytest.raises(ValueError):
        fee("polymarket_us", 2, .5, coefficient)


def test_missing_bid_is_not_zero_equity_and_complete_samples_have_coverage():
    trade = fill(candidate())
    missing = sample_equity([trade], {}, 4000, {})
    assert missing["unmarked_positions"] == 1 and "max_sampled_bid_drawdown_usd" not in missing
    marked = sample_equity([trade], {"forecast": {"at": 3999, "bid": .2}}, 4000, missing)
    assert marked["max_sampled_bid_drawdown_usd"] == pytest.approx(.55)
    assert marked["samples"] == 1 and marked["checks"] == 2


def test_state_rule_hash_and_restart_guard(tmp_path):
    state = paper.fresh_state(3500)
    state["portfolios"][BASELINE.name]["trades"] = [fill(candidate())]
    paper.save(state, tmp_path)
    restored = json.loads(gzip.decompress((tmp_path/"paper-state.json.gz").read_bytes()))
    paper.validate_state(restored)
    assert select([candidate(provider="polymarket_us")], [t["fixture_id"] for t in restored["portfolios"][BASELINE.name]["trades"]]) == []
    restored["portfolios"][BASELINE.name]["rules_hash"] = "changed"
    with pytest.raises(ValueError, match="RULES_CHANGED"):
        paper.validate_state(restored)


def test_fixture_matching_reversed_teams_aliases_props_and_ambiguity():
    def row(provider, title, native, start=1800):
        return {"provider": provider, "event_id": native, "event_title": title, "game_start": paper.iso(start)}
    rows = [row("kalshi", "Carolina vs Calgary", "k"), row("polymarket_us", "CGY Flames vs. CAR Hurricanes", "p"),
            row("kalshi", "Carolina vs Calgary: Exact score", "k-prop")]
    registry = {}
    result = paper.identities(rows, registry)
    assert len(set(result.values())) == 1 and len(result) == 3
    assert paper.identities([rows[0]], registry)["kalshi:k"] == result["polymarket_us:p"]
    assert paper.identities([row("kalshi", "Unknown vs Opponent", "x")], {}) == {}
    assert paper.identities([rows[0], row("polymarket_us", "Calgary vs Carolina", "p", 40000)], {}) == {}


def test_invalid_checkpoint_archive_never_extracts_files():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as z:
        z.writestr("../paper-state.json.gz", gzip.compress(b"{}"))
    with pytest.raises(ValueError, match="MEMBERS"):
        paper.compressed_json(stream.getvalue(), "paper-state.json.gz")


def test_singles_tennis_surnames_match_only_unique_opponents():
    def row(provider, title, native, league):
        return {"provider": provider, "event_id": native, "event_title": title,
                "game_start": paper.iso(1800), "league": league}
    k = row("kalshi", "Kecmanovic vs Duckworth", "k", "KXATPMATCH")
    p = row("polymarket_us", "Miomir Kecmanovic vs. James Duckworth", "p", "ATP")
    assert len(set(paper.identities([k, p], {}).values())) == 1
    ambiguous = row("polymarket_us", "Another Kecmanovic vs Other Duckworth", "q", "ATP")
    assert paper.identities([k, p, ambiguous], {}) == {}


def test_temporary_feed_failure_preserves_unprocessed_hour(tmp_path):
    state = paper.fresh_state(3500)
    with patch.object(paper, "restore", return_value=state), patch.object(paper, "artifacts", return_value=[]), \
         patch.object(paper, "snapshots_at", side_effect=RuntimeError("TEMPORARY_FEED_FAILURE")):
        with pytest.raises(RuntimeError, match="RETRY_REQUIRED"):
            paper.run(tmp_path, now=3800)
    saved = json.loads(gzip.decompress((tmp_path/"paper-state.json.gz").read_bytes()))
    assert saved["next_hour"] == 3600 and not saved["portfolios"][BASELINE.name]["trades"]


def test_next_minute_recorded_quote_and_first_confirmed_settlement():
    row = {"id": "forecast", "provider": "polymarket_us", "symbol": "game", "contract_id": "long", "side": "long",
           "event_id": "p", "event_title": "Alpha vs Beta", "game_start": paper.iso(1800), "forecast_end": paper.iso(10000),
           "predictions": [{"timestamp": paper.iso(3600), "quantiles": {"0.01": .6}}], "artifact_id": 1, "publication_ts": 3500}
    other = {**row, "provider": "kalshi", "event_id": "k", "predictions": []}
    points = [{"at": 3600, "ask": .5, "bid": .4}, {"at": 3660, "ask": .5, "bid": .4}]
    with patch.object(paper, "prices", return_value=points), patch.object(paper, "market", return_value={"endDate": paper.iso(9000), "feeCoefficient": ".0695"}):
        candidates, counts = paper.candidates_at([row, other], 3600, {})
    assert len(candidates) == 1 and candidates[0]["decision_at"] == 3660
    state = paper.fresh_state(3500)
    state["portfolios"][BASELINE.name]["trades"] = [fill(candidates[0])]
    with patch.object(paper, "market", return_value={}), patch.object(paper, "public_json", return_value={"settlement": "1"}):
        assert paper.update_settlements(state, 9500) == {}
    assert state["portfolios"][BASELINE.name]["trades"][0]["settlement_at"] == 9500


def test_crossed_out_of_range_and_future_book_points_are_excluded():
    valid = {"at": 3660, "ask": .5, "bid": .4}
    assert paper.valid_quotes([valid, {**valid, "at": 3700}, {**valid, "bid": .6},
                               {**valid, "ask": 1.2}, {**valid, "bid": -.1}], 3600, 3660) == [valid]
