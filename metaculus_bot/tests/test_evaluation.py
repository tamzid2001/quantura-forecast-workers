from copy import deepcopy
import math
import json
import sys

import pytest

from metaculus_bot.evaluation import (QUANTILES, binary_metrics, evaluate,
                                     select_temperature, temperature_probability)

AS_OF = "2026-03-01T00:00:00Z"


def row(identifier=1, **changes):
    return {"question_id": identifier, "question_type": "binary", "mode": "prospective",
            "forecast_at": "2026-01-01T01:00:00Z", "evidence_available_at": "2026-01-01T00:00:00Z",
            "close_time": "2026-01-02T00:00:00Z", "resolved_at": "2026-01-03T00:00:00Z",
            "answer": {"probability_yes": .8}, "outcome": 1, **changes}


def test_binary_scores_match_hand_calculation_and_reliability():
    report = evaluate([row(), row(2, answer={"probability_yes": .2}, outcome=0)], as_of=AS_OF)
    metrics = report["groups"][0]
    assert metrics["brier"] == pytest.approx(.04)
    assert metrics["log_loss"] == pytest.approx(-math.log(.8))
    assert metrics["ece_10_bins"] == pytest.approx(.2)
    assert sum(b["n"] for b in metrics["reliability"]) == 2
    assert report["peer_score"] is None


def test_replay_and_prospective_predictions_are_never_pooled():
    report = evaluate([row(), row(2, mode="replay")], as_of=AS_OF)
    assert len(report["groups"]) == 2 and all(g["n"] == 1 for g in report["groups"])


@pytest.mark.parametrize("changes", [
    {"evidence_available_at": "2026-01-01T02:00:00Z"},
    {"forecast_at": "2026-01-02T00:00:00Z"},
    {"resolved_at": "2026-03-02T00:00:00Z"},
    {"forecast_at": "2026-01-01T01:00:00"},
    {"mode": "unknown"}, {"question_id": True},
    {"answer": {"abstain": True}}, {"outcome": True},
    {"answer": {"probability_yes": math.nan}}, {"answer": {"probability_yes": 1.1}},
])
def test_bad_provenance_or_predictions_fail_closed(changes):
    with pytest.raises(ValueError):
        evaluate([row(**changes)], as_of=AS_OF)


def test_duplicate_games_questions_cannot_inflate_sample_size():
    with pytest.raises(ValueError, match="DUPLICATE_QUESTION"):
        evaluate([row(), row()], as_of=AS_OF)


def test_multiclass_scoring_requires_exact_resolved_option_and_sum():
    example = row(question_type="multiple_choice", answer={"probabilities": {"A": .7, "B": .3}}, outcome="A")
    result = evaluate([example], as_of=AS_OF)["groups"][0]
    assert result["log_loss"] == pytest.approx(-math.log(.7))
    assert result["brier_sum_over_classes"] == pytest.approx(.18)
    for answer, outcome in [({"A": .7, "B": .2}, "A"), ({"A": .7, "B": .3}, "C")]:
        with pytest.raises(ValueError):
            evaluate([{**example, "answer": {"probabilities": answer}, "outcome": outcome}], as_of=AS_OF)


def test_numeric_scores_keep_units_separate_and_measure_intervals():
    points = [{"percentile": q, "value": v} for q, v in zip(QUANTILES, range(1, 8))]
    a = row(question_type="numeric", unit="USD", answer={"percentiles": points}, outcome=4)
    b = {**a, "question_id": 2, "unit": "percentage points"}
    groups = evaluate([a, b], as_of=AS_OF)["groups"]
    assert len(groups) == 2
    assert groups[0]["median_mae"] == 0 and groups[0]["p10_p90_coverage"] == 1
    expected = sum(max(q * (4 - v), (q - 1) * (4 - v)) for q, v in zip(QUANTILES, range(1, 8))) / 7
    assert groups[0]["mean_pinball"] == pytest.approx(expected)
    invalid = deepcopy(a); invalid["answer"]["percentiles"][0]["value"] = 100
    with pytest.raises(ValueError, match="CROSSING_QUANTILES"):
        evaluate([invalid], as_of=AS_OF)


@pytest.mark.parametrize("p", [0, 1e-300, .01, .5, .99, 1])
def test_temperature_identity_and_stability(p):
    assert temperature_probability(p, 1) == pytest.approx(p)
    assert 0 <= temperature_probability(p, .5) <= 1
    if 0 < p < 1 and p != .5:
        softened = temperature_probability(p, 2)
        assert p < softened < .5 if p < .5 else .5 < softened < p


def calibration_rows():
    train = [row(i + 1, answer={"probability_yes": .99}, outcome=int(i % 5 != 0)) for i in range(100)]
    valid = [row(i + 101, answer={"probability_yes": .99}, outcome=int(i % 5 != 0),
                 evidence_available_at="2026-02-01T00:00:00Z", forecast_at="2026-02-01T01:00:00Z",
                 close_time="2026-02-02T00:00:00Z", resolved_at="2026-02-03T00:00:00Z") for i in range(50)]
    return train, valid


def test_calibration_fits_past_labels_and_audits_later_validation_without_deploying():
    train, valid = calibration_rows()
    report = select_temperature(train, valid, as_of=AS_OF)
    assert report["temperature"] > 1 and report["validation_improved"]
    assert report["candidate"]["log_loss"] < report["baseline"]["log_loss"]
    assert len(report["data_sha256"]) == 64 and report["deployment"].startswith("disabled")


def test_calibration_declines_if_validation_does_not_improve():
    train, valid = calibration_rows()
    for r in valid:
        r["outcome"] = 1
    report = select_temperature(train, valid, as_of=AS_OF)
    assert not report["validation_improved"] and report["temperature"] == 1


def test_small_samples_and_label_leakage_cannot_optimize_calibration():
    train, valid = calibration_rows()
    with pytest.raises(ValueError, match="MINIMUM"):
        select_temperature(train[:5], valid[:5], as_of=AS_OF)
    train[-1]["resolved_at"] = "2026-02-02T00:00:00Z"
    with pytest.raises(ValueError, match="LABEL_LEAKAGE"):
        select_temperature(train, valid, as_of=AS_OF)


def test_date_outcomes_score_in_seconds_and_cli_writes_real_report(monkeypatch, tmp_path):
    from metaculus_bot import evaluation
    points = [{"percentile": q, "value": f"2026-02-0{i + 1}T00:00:00Z"} for i, q in enumerate(QUANTILES)]
    example = row(question_type="date", answer={"percentiles": points}, outcome="2026-02-04T00:00:00Z")
    source, output = tmp_path / "records.jsonl", tmp_path / "out/report.json"
    source.write_text(json.dumps(example) + "\n")
    monkeypatch.setattr(sys, "argv", ["evaluation", str(source), "--as-of", AS_OF, "--out", str(output)])
    evaluation.main()
    result = json.loads(output.read_text())
    assert result["questions"] == 1 and result["groups"][0]["units"] == "seconds"
    assert result["groups"][0]["median_mae"] == 0


@pytest.mark.parametrize("changes", [{"question_type": "unknown"},
    {"question_type": "numeric", "unit": ""}, {"answer": {"probability_yes": True}}])
def test_unsupported_scales_types_and_boolean_probabilities_are_rejected(changes):
    with pytest.raises(ValueError):
        evaluate([row(**changes)], as_of=AS_OF)


def test_extreme_forecasts_have_finite_reported_loss_without_mutating_probabilities():
    assert binary_metrics([(0, 1), (1, 0)])["log_loss"] == pytest.approx(-math.log(1e-15))
    with pytest.raises(ValueError):
        binary_metrics([])
    with pytest.raises(ValueError):
        temperature_probability(.5, 0)


def test_calibration_never_mixes_replay_and_prospective_rows():
    train, valid = calibration_rows(); valid[-1]["mode"] = "replay"
    with pytest.raises(ValueError, match="MIXED_CALIBRATION"):
        select_temperature(train, valid, as_of=AS_OF)
