"""Actual bid/ask, side, timing, deduplication and tail-policy regression tests."""
import unittest
from copy import deepcopy
from decimal import Decimal

from market_research.sports_research import data, filters, p99

HOUR = 1789905600


def result():
    curve = {"0.01": .1, "0.25": .3, "0.5": .4, "0.75": .5, "0.9": .6, "0.99": .7}
    return {"kickoff": HOUR-3600, "cutoff": HOUR-7200, "end": HOUR+3600,
            "assumed_available_at": HOUR-3600, "historical_publication": False,
            "predictions": [{"timestamp": HOUR, "quantiles": curve},
                            {"timestamp": HOUR+3600, "quantiles": curve}],
            "quantity_step": "1", "fee_coefficient": ".07",
            "settlement": {"affirmative_payout": 1, "settlement_at": HOUR+86400},
            "forecast": {"forecast_id": "saved-forecast",
                         "origin": HOUR-7200,
                         "models": [{"id": m, "status": "completed"} for m in sorted(p99.MODELS)],
                         "weighting": {"policy": "rolling_loss_selection_v2"}}}


def contract(**changes):
    return {"provider": "kalshi", "side": "yes", "fixture_id": "game:one", "symbol": "KXNFLGAME-A",
            "contract_id": "KXNFLGAME-A:yes", "event_title": "Alpha vs Beta",
            "market_close": HOUR+7200, **changes}


def points(signal=.8, follow=.8, bid=.6):
    return [{"at": HOUR, "bid": min(bid, signal), "ask": signal},
            {"at": HOUR+60, "bid": min(bid, follow), "ask": follow}]


class P99Tests(unittest.TestCase):
    def test_buys_complement_of_original_bid_and_pays_opposite_fee(self):
        rows, counts = p99.candidate_rows(contract(), result(), points())
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["side"], "no")
        self.assertEqual(Decimal(row["price"]), Decimal(".4"))
        self.assertEqual(Decimal(row["quantity"]), 2)
        self.assertEqual(Decimal(row["entry_cost"]), Decimal(".8"))
        self.assertGreater(Decimal(row["entry_fee"]), 0)
        self.assertEqual(row["payout"], "0")
        self.assertGreater(counts["missing_signal_quote"], 0)
        trade = p99.timed_trades(rows)[0]
        self.assertIsNone(trade.features["discount"])
        self.assertAlmostEqual(trade.features["overshoot"], .1)
        self.assertAlmostEqual(trade.features["signal_bid_margin"], -.1)
        self.assertEqual(trade.strategy, data.P99)
        self.assertLess(trade.profit(), 0)

    def test_original_no_buys_yes_and_polymarket_long_buys_short(self):
        rows, _ = p99.candidate_rows(contract(side="no"), result(), points())
        self.assertEqual((rows[0]["side"], rows[0]["payout"]), ("yes", "1"))
        rows, _ = p99.candidate_rows(contract(provider="polymarket_us", side="long"), result(), points())
        self.assertEqual((rows[0]["side"], rows[0]["payout"]), ("short", "0"))

    def test_ask_equality_is_not_a_crossing_and_next_minute_must_persist(self):
        for sample, reason in [(points(signal=.7), "not_above_hourly_p99"),
                               (points(follow=.69), "crossing_did_not_persist"),
                               (points()[:1], "missing_next_minute_quote")]:
            with self.subTest(reason=reason):
                rows, counts = p99.candidate_rows(contract(), result(), sample)
                self.assertFalse(rows)
                self.assertEqual(counts[reason], 1)

    def test_stale_signal_and_late_following_quote_are_not_interpolated(self):
        sample = points()
        sample[0]["at"] -= 60
        self.assertFalse(p99.candidate_rows(contract(), result(), sample)[0])
        sample = points()
        sample[1]["at"] += 1
        self.assertFalse(p99.candidate_rows(contract(), result(), sample)[0])

    def test_closed_resolved_and_unbuyable_markets_are_excluded(self):
        r = result()
        r["settlement"]["settlement_at"] = HOUR+59
        for row, forecast, sample, reason in [
                (contract(market_close=HOUR+60), result(), points(), "market_closed"),
                (contract(), r, points(), "already_settled"),
                (contract(), result(), points(bid=0), "unavailable_opposite_ask")]:
            with self.subTest(reason=reason):
                rows, counts = p99.candidate_rows(row, forecast, sample)
                self.assertFalse(rows)
                self.assertEqual(counts[reason], 1)

    def test_invalid_forecast_models_origin_and_curve_fail_closed(self):
        for change in ("models", "policy", "origin", "input_origin", "empty", "crossed", "duplicate", "missing_p99"):
            r = result()
            if change == "models":
                r["forecast"]["models"].pop()
            elif change == "policy":
                r["forecast"]["weighting"]["policy"] = "unverified"
            elif change == "origin":
                r["assumed_available_at"] = HOUR+1
            elif change == "input_origin":
                r["forecast"]["origin"] = HOUR+1
            elif change == "empty":
                r["predictions"] = []
            elif change == "crossed":
                r["predictions"][0]["quantiles"]["0.5"] = .9
            elif change == "duplicate":
                r["predictions"][1]["timestamp"] = HOUR
            else:
                r["predictions"][0]["quantiles"].pop("0.99")
            with self.subTest(change=change), self.assertRaises(ValueError):
                p99.candidate_rows(contract(), r, points())

    def test_interpolated_endpoint_is_not_an_hourly_signal(self):
        r = result()
        r["predictions"][0]["interpolated_model_point"] = True
        self.assertFalse(p99.candidate_rows(contract(), r, points())[0])

    def test_conflicting_duplicate_quotes_and_bad_quotes_are_removed(self):
        sample = points()+[{"at": HOUR, "bid": .4, "ask": .9},
                           {"at": HOUR+1, "bid": .9, "ask": .1},
                           {"at": True, "bid": 0, "ask": 1}]
        self.assertEqual(len(p99.clean_points(sample)), 1)
        self.assertFalse(p99.candidate_rows(contract(), result(), sample)[0])

    def test_future_quotes_do_not_change_pre_signal_volatility(self):
        history = [{"at": HOUR-i*60, "bid": .58+(i%3)*.01, "ask": .8+(i%3)*.01}
                   for i in range(31)]
        before = p99.candidate_rows(contract(), result(), history+points()[1:])[0][0]
        after = p99.candidate_rows(contract(), result(), history+points()[1:]+[
            {"at": HOUR+120, "bid": .01, "ask": .99}])[0][0]
        self.assertEqual(before["quote_change_variance"], after["quote_change_variance"])
        self.assertIsNotNone(before["quote_change_std"])
        self.assertNotIn("discount_quote_sigma", before)

    def test_outcomes_never_choose_venue_side_or_entry_time(self):
        first = p99.candidate_rows(contract(), result(), points())[0][0]
        later = {**first, "decision_at": HOUR+61, "payout": "1"}
        other = {**first, "provider": "polymarket_us", "payout": "1"}
        self.assertEqual(p99.select([later, other, first]), [first])
        changed = deepcopy(first)
        changed["payout"] = "1"
        self.assertEqual(p99.select([later, other, changed]), [changed])
        self.assertFalse(p99.select([{**first, "identity_verified": False}]))

    def test_unknown_credit_time_and_unsupported_quantity_side_payout_fail(self):
        rows = p99.candidate_rows(contract(), result(), points())[0]
        rows[0]["settlement_at"] = None
        with self.assertRaisesRegex(ValueError, "CREDIT_TIME"):
            p99.timed_trades(rows)
        for change in ("step", "payout", "side"):
            r, row = result(), contract()
            if change == "step":
                r["quantity_step"] = ".001"
            elif change == "payout":
                r["settlement"]["affirmative_payout"] = 2
            else:
                row["side"] = "short"
            with self.subTest(change=change), self.assertRaises(ValueError):
                p99.candidate_rows(row, r, points())

    def test_unsettled_rows_remain_open_and_bid_confirmation_is_strict(self):
        r = result()
        r["settlement"] = None
        row = p99.candidate_rows(contract(), r, points())[0][0]
        trade = p99.timed_trades([row])[0]
        self.assertFalse(trade.closed)
        veto = filters.EntryVeto("bid", require_bid_above_p99=True)
        self.assertEqual(veto.decision(trade), "veto")
        for margin, expected in ((None, "missing_feature"), (0, "veto"), (.01, "keep")):
            trade.features["signal_bid_margin"] = margin
            self.assertEqual(veto.decision(trade), expected)
        self.assertEqual(len(filters.candidates(data.P99)), 14)


if __name__ == "__main__":
    unittest.main()
