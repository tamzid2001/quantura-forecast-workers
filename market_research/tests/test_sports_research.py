"""Deterministic statistical and point-in-time controls; no exchange/model access."""
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
import zipfile
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from market_research.sports_research import (
    automation,
    cli,
    data,
    features,
    filters,
    prospective,
    statistics,
    study,
)

BASE = datetime(2026, 9, 20, tzinfo=timezone.utc).timestamp()


def test_tight_tracking_is_readable_without_amending_frozen_experiment():
    parsed = data.normalize(ledger_row(portfolio=data.TIGHT, p01=".56", signal_ask=".55"), "paper")
    assert parsed.strategy == data.TIGHT
    registration = prospective.initialize(BASE - 86400)
    measured = prospective.assess(registration, [trade(strategy=data.TIGHT)],
                                  {"paper": {"as_of": data.iso(BASE + 5000)}}, BASE + 5000, draws=100)
    assert "paper:" + data.TIGHT not in measured["families"]
    assert registration["design"]["sha256"] == "0a42ab4cd8e9b1fb223092d9ca082bffd2c477eb05eb36958148fd3bca991142"


def trade(index=0, payout=1, **kwargs):
    price = Decimal(str(kwargs.pop("price", ".5")))
    values = {"cohort": "paper", "strategy": data.WITHIN, "fixture": f"game:{index}", "provider": "kalshi",
                  "symbol": "KXNFLGAME-A", "title": "Alpha vs Beta", "entry_at": BASE + index * 86400,
                  "settled_at": BASE + index * 86400 + 3600, "quantity": Decimal(1), "cost": price,
                  "fee": Decimal(0), "planned_price": price, "payout": Decimal(str(payout)),
                  "features": {"planned_price": float(price), "entry_spread": .01, "discount": .05,
                            "fee_fraction": 0, "quote_change_std": .01, "quote_change_pairs": 30,
                            "discount_quote_sigma": 5}}
    values.update(kwargs)
    return data.Trade(**values)


def ledger_row(**kwargs):
    return {"portfolio": data.WITHIN, "fixture_id": "game:a", "provider": "kalshi",
            "symbol": "KXNFLGAME-A", "event_title": "Alpha vs Beta", "decision_at": str(BASE + 60),
            "signal_at": str(BASE), "quantity": "2", "entry_cost": "1", "entry_fee": ".04",
            "price": ".5", "payout": "1", "settlement_at": str(BASE + 3600),
            "p01": ".6", "signal_ask": ".55", "signal_entry_ask": ".5", "opposite_ask": ".51", **kwargs}


def archive(members):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, body in members.items():
            z.writestr(name, body)
    return out.getvalue()


class DataTests(unittest.TestCase):
    def test_money_preserves_decimal_precision(self):
        t = data.normalize(ledger_row(), "paper")
        self.assertEqual(t.profit(), Decimal(".96"))
        self.assertAlmostEqual(t.features["entry_spread"], .01)
        self.assertAlmostEqual(t.features["discount"], .05)

    def test_numbers_reject_nonfinite_boolean_and_none(self):
        for value in (True, None, "NaN", "Infinity", "x", "1e50"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                data.decimal(value, "money")

    def test_timestamp_requires_timezone_or_epoch(self):
        self.assertEqual(data.timestamp("2026-09-20T00:00:00Z"), BASE)
        self.assertEqual(data.timestamp(str(BASE)), BASE)
        for value in (True, None, "", "2026-09-20", "NaN", -1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                data.timestamp(value)

    def test_bad_cashflows_and_settlement_fail(self):
        for changes in ({"quantity": "0"}, {"entry_cost": "-1"}, {"entry_fee": "-.01"},
                        {"price": "1"}, {"payout": "2"}, {"settlement_at": str(BASE)},
                        {"payout": "", "settlement_at": str(BASE + 3600)}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                data.normalize(ledger_row(**changes), "paper")

    def test_future_features_are_refused(self):
        with self.assertRaisesRegex(ValueError, "FEATURE_LOOKAHEAD"):
            data.normalize(ledger_row(feature_cutoff_at=str(BASE + 1)), "paper")

    def test_unknown_identity_strategy_and_crossed_book(self):
        for changes in ({"fixture_id": ""}, {"portfolio": "unknown"}, {"provider": "other"},
                        {"opposite_ask": ".1"}, {"quote_change_std": "-.1"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                data.normalize(ledger_row(**changes), "paper")

    def test_unsettled_trade_cannot_create_profit(self):
        t = data.normalize(ledger_row(payout="", settlement_at=""), "paper")
        self.assertFalse(t.closed)
        self.assertEqual(statistics.summary([t])["open"], 1)
        with self.assertRaises(ValueError):
            t.profit()

    def test_partial_fill_uses_filled_quantity_and_does_not_infer_spread(self):
        row = ledger_row(filled=".5", entry_cost=".25", entry_fee=".01")
        row.pop("portfolio")
        row.pop("signal_ask")
        row.pop("signal_entry_ask")
        row.pop("opposite_ask")
        t = data.normalize(row, "limit")
        self.assertEqual(t.quantity, Decimal(".5"))
        self.assertEqual(t.profit(), Decimal(".24"))
        self.assertIsNone(t.features["entry_spread"])
        self.assertIsNone(t.features["discount"])

    def test_duplicate_csv_header_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "input.csv"
            path.write_text("quantity,quantity\n1,2\n")
            with self.assertRaises(ValueError):
                data.read_csv(path)

    def test_real_loader_preserves_sources_and_rejects_duplicate_games(self):
        import csv
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "paper"
            root.mkdir()
            row = ledger_row()
            path = root / "comparison-ledger.csv"
            def write(rows):
                with path.open("w", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=list(row))
                    writer.writeheader()
                    writer.writerows(rows)
            write([row])
            (root / "stats.json").write_text(json.dumps({"mode": "paper_only", "as_of": data.iso(BASE + 4000)}))
            trades, sources = data.load_inputs(root.parent)
            self.assertEqual(len(trades), 1)
            self.assertEqual(sources["month"]["status"], "missing")
            write([row, row])
            with self.assertRaisesRegex(ValueError, "DUPLICATE_GAME"):
                data.load_inputs(root.parent)
            write([row])
            (root / "stats.json").write_text(json.dumps({"mode": "live"}))
            with self.assertRaisesRegex(ValueError, "EXPECTED_PAPER_MODE"):
                data.load_inputs(root.parent)


class StatisticsTests(unittest.TestCase):
    def test_sample_size_is_explicitly_iid_planning_not_achieved_power(self):
        result = statistics.independent_sample_planning(.5)
        self.assertEqual(result["approximate_independent_games"], 785)
        self.assertIn("IID", result["assumptions"])
        for changes in ({"sigma": 0}, {"sigma": float("nan")}, {"sigma": .5, "effect_usd": 0},
                        {"sigma": .5, "alpha": 1}, {"sigma": .5, "power": .5}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                statistics.independent_sample_planning(**changes)

    def test_cashflow_weighted_break_even_is_not_average_trade_win_rate(self):
        rows = [trade(0, 1, quantity=Decimal(2), cost=Decimal(".8"), fee=Decimal(".04")),
                trade(1, 0, quantity=Decimal(10), cost=Decimal(1), fee=Decimal(".06"))]
        result = statistics.summary(rows)
        self.assertAlmostEqual(result["cashflow_break_even_payout_fraction"], 1.9/12)
        self.assertAlmostEqual(result["cashflow_weighted_actual_payout_fraction"], 2/12)
        self.assertEqual(result["win_rate_pct"], 50)

    def test_variance_std_pnl_streaks_and_drawdown(self):
        rows = [trade(0, 0, quantity=Decimal(2), cost=Decimal(1)),
                trade(1, .5, quantity=Decimal(2), cost=Decimal(1)),
                trade(2, 1, quantity=Decimal(2), cost=Decimal(1))]
        s = statistics.summary(rows)
        self.assertEqual((s["wins"], s["losses"], s["breakevens"]), (1, 1, 1))
        self.assertEqual(s["net_pnl_usd"], 0)
        self.assertEqual(s["variance_net_per_game_usd2"], 1)
        self.assertEqual(s["std_net_per_game_usd"], 1)
        self.assertEqual(s["max_realized_drawdown_usd"], 1)
        self.assertEqual(s["binary_calibration_n"], 2)
        self.assertEqual(s["entry_quote_brier_baseline"], .25)

    def test_stress_is_per_filled_contract_and_includes_extra_spend(self):
        t = trade(quantity=Decimal(10), cost=Decimal(5), fee=Decimal(".1"))
        s = statistics.summary([t], .02)
        self.assertAlmostEqual(s["net_pnl_usd"], 4.7)
        self.assertAlmostEqual(s["closed_spend_usd"], 5.3)
        self.assertAlmostEqual(s["roi_pct"], 100 * 4.7 / 5.3)
        for cost in (-.01, float("nan"), True):
            with self.assertRaises(ValueError):
                statistics.summary([t], cost)

    def test_empty_and_single_sample_are_not_zero_variance_evidence(self):
        self.assertIsNone(statistics.summary([])["roi_pct"])
        self.assertIsNone(statistics.summary([trade()])["variance_net_per_game_usd2"])
        self.assertIsNone(statistics.summary([])["block_sign_p_positive_median"])

    def test_quantile_interpolation_and_invalid_inputs(self):
        self.assertEqual(statistics.quantile([1, 3], .25), 1.5)
        self.assertEqual(statistics.quantile([3], 1), 3)
        for values, p in [([], .5), ([1], -1), ([float("nan")], .5)]:
            with self.assertRaises(ValueError):
                statistics.quantile(values, p)

    def test_wilson_boundary_matches_known_formula(self):
        self.assertAlmostEqual(statistics.wilson(10, 10)[0], .7224672001371107)
        self.assertAlmostEqual(statistics.wilson(0, 10)[1], .2775327998628892)
        self.assertIsNone(statistics.wilson(0, 0))
        with self.assertRaises(ValueError):
            statistics.wilson(True, 10)

    def test_exact_sign_tail_and_null_interpretation(self):
        self.assertAlmostEqual(statistics.sign_p_value([1, 1, 1, 1]), 1 / 16)
        self.assertAlmostEqual(statistics.sign_p_value([1, 1, 1, -100]), 5 / 16)
        self.assertEqual(statistics.sign_p_value([-1, -2]), 1)
        self.assertIsNone(statistics.sign_p_value([0, 0]))
        with self.assertRaises(ValueError):
            statistics.sign_p_value([float("nan")])

    def test_multiple_comparisons_do_not_assume_independence(self):
        self.assertAlmostEqual(statistics.bonferroni(.01, 3), .03)
        self.assertEqual(statistics.bonferroni(.9, 3), 1)
        self.assertIsNone(statistics.bonferroni(None, 3))
        with self.assertRaises(ValueError):
            statistics.bonferroni(.1, 0)

    def test_moving_blocks_are_reproducible_and_preserve_adjacency(self):
        a = list(statistics.block_indices(12, 3, 17))
        self.assertEqual(a, list(statistics.block_indices(12, 3, 17)))
        self.assertEqual(len(a[0]), 12)
        self.assertEqual((a[0][0] + 1) % 12, a[0][1])
        self.assertEqual(statistics.blocks([1, 2, 3, 4]), [6])
        self.assertEqual(statistics.blocks([1, 2]), [])

    def test_block_bootstrap_refuses_tiny_day_sample(self):
        rows = [trade(i) for i in range(7)]
        self.assertEqual(statistics.bootstrap_comparison(rows, rows, 100)["status"], "insufficient_trading_days")
        with self.assertRaises(ValueError):
            statistics.bootstrap_comparison(rows, rows, 1)

    def test_block_bootstrap_reproducibility_and_paired_veto(self):
        baseline = [trade(i, i % 2) for i in range(20)]
        selected = [t for t in baseline if t.payout == 1]  # oracle only to pin the math; never a selectable filter
        a = statistics.bootstrap_comparison(baseline, selected, 200, 7)
        self.assertEqual(a, statistics.bootstrap_comparison(baseline, selected, 200, 7))
        self.assertGreater(a["mean_daily_improvement_ci95_usd"][0], 0)
        self.assertTrue(a["roi_ci95_pct"][0] > 0)

    def test_zero_exposure_resamples_are_explicit(self):
        rows = [trade(i) for i in range(20)]
        result = statistics.bootstrap_comparison(rows, rows[:1], 500, 9)
        self.assertEqual(result["status"], "insufficient_resamples_with_exposure")
        self.assertIsNone(result["roi_ci95_pct"])

    def test_constant_return_bootstrap_is_marked_degenerate(self):
        rows = [trade(i) for i in range(10)]
        self.assertEqual(statistics.bootstrap_comparison(rows, rows, 100)["status"], "degenerate_roi_bootstrap")


class FeatureFilterTests(unittest.TestCase):
    def test_future_quotes_cannot_change_past_volatility(self):
        hour = 3600
        past = [{"at": 60 * i, "bid": .3 + .001 * (i % 2), "ask": .32 + .001 * (i % 2)} for i in range(1, 61)]
        a = features.capture(past, hour, .6, .4)
        b = features.capture(past + [{"at": 3660, "bid": .01, "ask": .99}], hour, .6, .4)
        self.assertEqual(a, b)
        self.assertEqual(a["quote_change_pairs"], 59)
        self.assertGreater(a["quote_change_variance"], 0)
        self.assertGreater(a["discount_quote_sigma"], 0)

    def test_missing_minutes_not_bridged_and_conflicting_duplicates_removed(self):
        points = [{"at": 120 * i, "bid": .2, "ask": .4} for i in range(30)]
        result = features.capture(points, 3600, .6, .4)
        self.assertEqual(result["quote_change_pairs"], 0)
        self.assertIsNone(result["quote_change_std"])
        result = features.capture([{"at": 3600, "bid": .2, "ask": .4},
                                   {"at": 3600, "bid": .4, "ask": .6}], 3600, .6, .4)
        self.assertIsNone(result["quote_change_std"])

    def test_zero_sigma_never_creates_infinite_confidence(self):
        points = [{"at": 60 * i, "bid": .2, "ask": .4} for i in range(1, 61)]
        result = features.capture(points, 3600, .6, .4, {"0.01": .2, "0.25": .3, "0.75": .7, "0.99": .8})
        self.assertEqual(result["quote_change_std"], 0)
        self.assertIsNone(result["discount_quote_sigma"])
        self.assertAlmostEqual(result["forecast_iqr"], .4)
        self.assertAlmostEqual(result["forecast_tail_width"], .6)

    def test_crossing_quantiles_and_bad_quotes_do_not_invent_features(self):
        result = features.capture([{"at": 3600, "bid": float("nan"), "ask": .5}], 3600, .6, .4,
                                  {"0.25": .8, "0.75": .2})
        self.assertIsNone(result["forecast_iqr"])
        self.assertIsNone(features.finite(True))

    def test_fixed_family_and_fingerprints(self):
        within = filters.candidates(data.WITHIN)
        outside = filters.candidates(data.OUTSIDE)
        self.assertEqual(len(within), 14)
        self.assertEqual(len({f.name for f in within}), 14)
        self.assertNotEqual(within[6].name, outside[6].name)
        self.assertEqual(within[1].fingerprint(), filters.candidates(data.WITHIN)[1].fingerprint())

    def test_filters_do_not_use_outcomes(self):
        a = trade()
        b = replace(a, payout=Decimal(0))
        for f in filters.candidates(a.strategy):
            self.assertEqual(f.decision(a), f.decision(b))

    def test_missing_sigma_is_missing_not_a_zero_risk_trade(self):
        t = replace(trade(), features={"planned_price": .5})
        self.assertEqual(filters.EntryVeto("sigma", quote_sigma_cap=.02).decision(t), "missing_feature")
        self.assertEqual(filters.EntryVeto("sigma", quote_sigma_cap=.02).decision(
            replace(trade(), features={"quote_change_std": .01, "quote_change_pairs": 2})), "missing_feature")

    def test_prop_exclusion_and_price_spread_boundaries(self):
        prop = replace(trade(), symbol="KXATPEXACTMATCH-A", title="Alpha vs Beta: Exact Match Score")
        self.assertTrue(filters.obvious_prop(prop))
        self.assertEqual(filters.EntryVeto("props", exclude_obvious_props=True).decision(prop), "veto")
        f = filters.EntryVeto("price", price_cap=.5, spread_cap=.01)
        self.assertEqual(f.decision(trade()), "keep")
        self.assertEqual(f.decision(trade(price=".51")), "veto")


class StudyTests(unittest.TestCase):
    def test_split_purges_labels_that_arrived_after_cutoff(self):
        cutoff = BASE + 2 * 86400
        early, late = trade(0), replace(trade(1), settled_at=cutoff + 1)
        train, val, purge = study.split([early, late, trade(3)], cutoff)
        self.assertEqual(train, [early])
        self.assertEqual(purge, [late])
        self.assertEqual(val, [trade(3)])

    def test_game_cannot_appear_on_both_sides(self):
        with self.assertRaisesRegex(ValueError, "GAME_LEAKAGE"):
            study.split([trade(0), replace(trade(3), fixture="game:0")], BASE + 86400)

    def test_choice_needs_evidence_not_one_profitable_trade(self):
        item = study.comparison([trade()], filters.EntryVeto("baseline"), 100)
        self.assertIsNone(study.training_choice([item]))

    def test_real_study_and_report_do_not_call_empty_samples_profitable(self):
        rows = [trade(i, i % 2) for i in range(30)]
        result = study.analyze(rows, {}, 100, 42)
        cohort = result["cohorts"]["paper:" + data.WITHIN]
        self.assertEqual(cohort["baseline"]["settled"], 30)
        self.assertFalse(result["automatic_live_promotion"])
        self.assertFalse(result["price_quantile_is_settlement_probability"])
        self.assertIn("P01 is a market-price quantile", study.markdown(result))


class ProspectiveTests(unittest.TestCase):
    def test_legacy_design_amendment_is_audited_and_cannot_move_or_restart_dates(self):
        state = prospective.initialize(BASE+100)
        original_dates = {k: state[k] for k in ("created_at", "starts_at", "ends_at", "evaluate_after")}
        legacy = dict(state["design"])
        legacy.pop("sign_test_tail")
        legacy.pop("sha256")
        legacy["sha256"] = hashlib.sha256(json.dumps(legacy, sort_keys=True).encode()).hexdigest()
        state = {**state, "design": legacy}
        restored = prospective.restore(state, BASE+101)
        self.assertEqual({k: restored[k] for k in original_dates}, original_dates)
        self.assertEqual(restored["amendments"][0]["previous_design_sha256"], legacy["sha256"])
        self.assertEqual(prospective.restore(restored, BASE+86400), restored)
        with self.assertRaisesRegex(ValueError, "DESIGN_CHANGED"):
            prospective.restore(state, BASE+86400)
        tampered = {**state, "design": {**legacy, "days": 1}}
        with self.assertRaises(ValueError):
            prospective.restore(tampered, BASE+101)

    def test_registration_freezes_next_day_and_sixty_day_window(self):
        state = prospective.initialize(BASE + 12)
        self.assertEqual(data.timestamp(state["starts_at"]), BASE + 86400)
        self.assertEqual(data.timestamp(state["ends_at"]) - data.timestamp(state["starts_at"]), 60 * 86400)
        self.assertEqual(prospective.validate(state), state)

    def test_changed_design_or_dates_cannot_reset_window(self):
        state = prospective.initialize(BASE)
        state["ends_at"] = data.iso(BASE + 80 * 86400)
        with self.assertRaises(ValueError):
            prospective.validate(state)
        state = prospective.initialize(BASE)
        state["design"]["alpha"] = 1
        with self.assertRaises(ValueError):
            prospective.validate(state)

    def test_daily_looks_do_not_promote_or_spend(self):
        state = prospective.initialize(BASE)
        rows = [trade(1)]
        result = prospective.assess(state, rows, {}, BASE + 2 * 86400, 100)
        self.assertFalse(result["automatic_live_activation"])
        decisions = {x["decision"] for v in result["families"].values() for x in v}
        self.assertEqual(decisions, {"reference_only", "collecting_until_fixed_deadline"})

    def test_future_labels_are_refused(self):
        state = prospective.initialize(BASE)
        with self.assertRaisesRegex(ValueError, "FUTURE_DATA"):
            prospective.assess(state, [trade(10)], {}, BASE + 86400, 100)
        with self.assertRaises(ValueError):
            prospective.assess(state, [], {}, BASE - 1, 100)

    def test_deadline_does_not_override_missing_source_and_open_games(self):
        state = prospective.initialize(BASE)
        result = prospective.assess(state, [replace(trade(1), payout=None, settled_at=None)], {},
                                    BASE + 70 * 86400, 100)
        candidate = result["families"]["paper:" + data.WITHIN][1]
        self.assertEqual(candidate["decision"], "not_established")
        self.assertIn("SOURCE_NOT_CAUGHT_UP_THROUGH_END", candidate["deficits"])
        self.assertIn("UNSETTLED_BASELINE_GAMES", candidate["deficits"])


class AutomationTests(unittest.TestCase):
    def test_gh_wrapper_preserves_binary_and_rejects_failures_and_large_responses(self):
        ok = subprocess.CompletedProcess([], 0, stdout=b'{"ok":true}')
        with patch.object(automation.subprocess, "run", return_value=ok) as run:
            self.assertEqual(automation.gh("actions/artifacts"), {"ok": True})
            self.assertEqual(automation.gh("actions/artifacts", True), ok.stdout)
            self.assertEqual(run.call_args.args[0][:2], ["gh", "api"])
            self.assertEqual(run.call_args.kwargs["timeout"], 90)
        for value, expected in ((subprocess.CompletedProcess([], 1, stdout=b""), RuntimeError),
                                (subprocess.CompletedProcess([], 0, stdout=b"x"*(8*1024*1024+1)), ValueError)):
            with patch.object(automation.subprocess, "run", return_value=value), self.assertRaises(expected):
                automation.gh("actions/artifacts")

    def test_latest_requires_trusted_completed_workflow_and_accepts_recovery_checkpoint(self):
        artifact = {"id": 5, "name": "expected", "created_at": data.iso(BASE), "expired": False,
                    "workflow_run": {"head_branch": "main", "id": 2}}
        run = {"repository": {"full_name": automation.REPOSITORY}, "head_branch": "main",
               "event": "schedule", "path": automation.WORKFLOW, "status": "completed", "conclusion": "success"}
        with patch.object(automation, "gh", side_effect=[{"artifacts": [artifact]}, run]):
            self.assertEqual(automation.latest("expected", automation.WORKFLOW), artifact)
        with patch.object(automation, "gh", side_effect=[{"artifacts": [artifact]}, {**run, "conclusion": "failure"}]):
            self.assertIsNone(automation.latest("expected", automation.WORKFLOW))
        with patch.object(automation, "gh", side_effect=[{"artifacts": [artifact]}, {**run, "conclusion": "failure"}]):
            self.assertEqual(automation.latest("expected", automation.WORKFLOW, True), artifact)
        with patch.object(automation, "gh", return_value={"artifacts": [{**artifact, "expired": True}]}):
            self.assertIsNone(automation.latest("expected", automation.WORKFLOW))
        for changes in ({"event": "pull_request"}, {"head_branch": "untrusted"},
                        {"path": "other.yml"}, {"repository": {"full_name": "other/repo"}}):
            with (self.subTest(changes=changes),
                  patch.object(automation, "gh", side_effect=[{"artifacts": [artifact]}, {**run, **changes}]),
                  self.assertRaisesRegex(ValueError, "PROVENANCE")):
                automation.latest("expected", automation.WORKFLOW)

    def test_collect_writes_receipts_and_requires_core_members(self):
        artifact = {"id": 5, "created_at": data.iso(BASE)}
        packed = archive({"stats.json": b'{}', "comparison-ledger.csv": b"test\n1\n"})
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(automation, "latest", side_effect=[artifact, None, None]), patch.object(automation, "gh", return_value=packed):
                receipts = automation.collect(temp)
            self.assertEqual(receipts["limit"]["status"], "unavailable")
            self.assertEqual((Path(temp)/"paper/comparison-ledger.csv").read_bytes(), b"test\n1\n")
            self.assertEqual(json.loads((Path(temp)/"paper/artifact.json").read_text())["id"], 5)
            with patch.object(automation, "latest", return_value=None):
                automation.collect(temp)
            trades, sources = data.load_inputs(temp)
            self.assertFalse(trades)
            self.assertEqual(sources["paper"]["status"], "missing")
            with (patch.object(automation, "latest", return_value=artifact),
                  patch.object(automation, "gh", return_value=archive({"stats.json": b'{}'})),
                  self.assertRaisesRegex(ValueError, "REQUIRED_RESEARCH_INPUT")):
                automation.collect(temp)

    def test_automation_main_writes_report_and_checkpoints_registration_before_failure(self):
        now = BASE+4000
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(automation.time, "time", return_value=now), patch.object(automation, "registry", return_value=prospective.initialize(now)), patch.object(automation, "collect", return_value={}):
                self.assertEqual(automation.main(["--output", temp, "--draws", "100"]), 0)
            self.assertIn("Frozen prospective", (Path(temp)/"REPORT.md").read_text())
            self.assertFalse(json.loads((Path(temp)/"research.json").read_text())["automatic_live_promotion"])
            with (patch.object(automation.time, "time", return_value=now),
                  patch.object(automation, "registry", return_value=prospective.initialize(now)),
                  patch.object(automation, "collect", side_effect=RuntimeError("source failed")),
                  self.assertRaisesRegex(RuntimeError, "source failed")):
                automation.main(["--output", temp, "--draws", "100"])
            self.assertEqual(json.loads((Path(temp)/"registry.json").read_text())["created_at"], data.iso(now))

    def test_cli_runs_real_normalization_math_and_refuses_overwriting_inputs(self):
        import csv
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            inputs = root/"inputs/paper"
            inputs.mkdir(parents=True)
            row = ledger_row()
            with (inputs/"comparison-ledger.csv").open("w") as f:
                writer = csv.DictWriter(f, fieldnames=list(row))
                writer.writeheader()
                writer.writerow(row)
            (inputs/"stats.json").write_text(json.dumps({"mode": "paper_only", "as_of": data.iso(BASE+4000)}))
            out = root/"output"
            self.assertEqual(cli.main(["--inputs", str(inputs.parent), "--output", str(out), "--draws", "100"]), 0)
            report = json.loads((out/"research.json").read_text())
            self.assertAlmostEqual(report["cohorts"]["paper:"+data.WITHIN]["baseline"]["net_pnl_usd"], .96)
            with self.assertRaises(SystemExit):
                cli.main(["--inputs", str(inputs.parent), "--output", str(inputs.parent)])

    def test_archive_never_extracts_paths_or_accepts_unexpected_members(self):
        self.assertEqual(automation.unpack(archive({"stats.json": b"{}"}), {"stats.json"}), {"stats.json": b"{}"})
        for members in ({"../stats.json": b"{}"}, {"other.json": b"{}"}, {}):
            with self.subTest(members=members), self.assertRaises(ValueError):
                automation.unpack(archive(members), {"stats.json"})

    def test_health_tracks_actual_replay_horizon_not_just_fresh_upload(self):
        result = automation.health({"paper": {"status": "available", "as_of": data.iso(BASE),
                                               "next_hour": data.iso(BASE - 4 * 3600)},
                                    "month": {"status": "available", "study_complete": False},
                                    "limit": {"status": "missing"}}, BASE + 10)
        self.assertIn("HOURLY_REPLAY_NOT_CAUGHT_UP", result["paper"]["warnings"])
        self.assertIn("HISTORICAL_REPLAY_PARTIAL", result["month"]["warnings"])
        self.assertIn("SOURCE_UNAVAILABLE", result["limit"]["warnings"])
        result = automation.health({"paper": {"status": "available", "as_of": data.iso(BASE+3600)},
                                    "limit": {"status": "available", "as_of": data.iso(BASE-10*3600)}}, BASE)
        self.assertIn("SOURCE_TIMESTAMP_IN_FUTURE", result["paper"]["warnings"])
        self.assertIn("SOURCE_STATISTICS_STALE", result["limit"]["warnings"])

    def test_first_registration_and_missing_existing_registration(self):
        with patch.object(automation, "latest", return_value=None), patch.object(automation, "gh", return_value={"workflow_runs": []}):
            self.assertEqual(automation.registry(BASE)["created_at"], data.iso(BASE))
        with (patch.object(automation, "latest", return_value=None),
              patch.object(automation, "gh", return_value={"workflow_runs": [{}]}),
              self.assertRaisesRegex(RuntimeError, "CANNOT_SILENTLY_RESTART")):
            automation.registry(BASE)

    def test_failed_run_registry_checkpoint_is_restorable(self):
        artifact = {"id": 1}
        packed = archive({"registry.json": json.dumps(prospective.initialize(BASE)).encode()})
        with patch.object(automation, "latest", return_value=artifact) as latest, patch.object(automation, "gh", return_value=packed):
            self.assertEqual(automation.registry(BASE)["created_at"], data.iso(BASE))
            latest.assert_called_once_with("quantura-sports-research-registry-v1", automation.WORKFLOW, checkpoint=True)


if __name__ == "__main__":
    unittest.main()
