"""Offline proper scoring and chronological calibration; never submits forecasts.

Input JSONL is a join of immutable forecasts and independently obtained outcomes.
Timestamp checks reject observable leakage, but cannot establish that an LLM's
pretraining excluded historical outcomes. Replay and prospective rows stay separate.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path

QUANTILES = (.01, .1, .25, .5, .75, .9, .99)


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("EXPLICIT_TIMESTAMP_REQUIRED")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("TIMEZONE_REQUIRED")
    return result.astimezone(timezone.utc)


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("FINITE_NUMBER_REQUIRED")
    return float(value)


def probability(value):
    value = number(value)
    if not 0 <= value <= 1:
        raise ValueError("PROBABILITY_OUT_OF_RANGE")
    return value


def validate_records(records, *, as_of):
    cutoff, seen = timestamp(as_of), set()
    for row in records:
        identity = row["question_id"]
        if isinstance(identity, bool) or not isinstance(identity, int) or identity <= 0:
            raise ValueError("QUESTION_ID_REQUIRED")
        if identity in seen:
            raise ValueError("DUPLICATE_QUESTION")
        seen.add(identity)
        forecast, evidence, close, resolved = [timestamp(row[k]) for k in
            ("forecast_at", "evidence_available_at", "close_time", "resolved_at")]
        if not evidence <= forecast < close <= resolved <= cutoff:
            raise ValueError("AS_OF_ORDER_VIOLATION")
        if row.get("mode") not in {"prospective", "replay"}:
            raise ValueError("EVALUATION_MODE_REQUIRED")
        if row["question_type"] not in {"binary", "multiple_choice", "numeric", "discrete", "date"}:
            raise ValueError("UNSUPPORTED_SCORING_TYPE")
        if row["answer"].get("abstain"):
            raise ValueError("ABSTENTION_IS_NOT_A_SCORED_FORECAST")
    return records


def negative_log(value):
    # Numerical reporting floor only. Forecasts themselves are never clipped.
    return -math.log(max(value, 1e-15))


def binary_metrics(pairs):
    if not pairs:
        raise ValueError("NONEMPTY_BINARY_SAMPLE_REQUIRED")
    bins = []
    for index in range(10):
        selected = [(p, y) for p, y in pairs if min(int(p * 10), 9) == index]
        if selected:
            bins.append({"lower": index / 10, "upper": (index + 1) / 10,
                         "n": len(selected), "mean_probability": sum(p for p, _ in selected) / len(selected),
                         "observed_rate": sum(y for _, y in selected) / len(selected)})
    n = len(pairs)
    return {"n": n, "brier": sum((p - y) ** 2 for p, y in pairs) / n,
            "log_loss": sum(negative_log(p if y else 1 - p) for p, y in pairs) / n,
            "ece_10_bins": sum(b["n"] * abs(b["mean_probability"] - b["observed_rate"]) for b in bins) / n,
            "reliability": bins}


def binary_pairs(records):
    result = []
    for row in records:
        if row["question_type"] != "binary" or type(row["outcome"]) is not int or row["outcome"] not in (0, 1):
            raise ValueError("BINARY_RESOLUTION_REQUIRED")
        result.append((probability(row["answer"]["probability_yes"]), row["outcome"]))
    return result


def multiclass_metrics(records):
    losses, briers = [], []
    for row in records:
        values, outcome = row["answer"]["probabilities"], row["outcome"]
        if not isinstance(values, dict) or len(values) < 2 or outcome not in values:
            raise ValueError("EXACT_RESOLVED_OPTION_REQUIRED")
        values = {key: probability(value) for key, value in values.items()}
        if abs(sum(values.values()) - 1) > 1e-9:
            raise ValueError("NORMALIZED_DISTRIBUTION_REQUIRED")
        losses.append(negative_log(values[outcome]))
        briers.append(sum((p - int(key == outcome)) ** 2 for key, p in values.items()))
    return {"n": len(records), "log_loss": sum(losses) / len(records),
            "brier_sum_over_classes": sum(briers) / len(records)}


def numeric_metrics(records):
    losses, errors, intervals = [], [], []
    for row in records:
        points = row["answer"]["percentiles"]
        if tuple(p["percentile"] for p in points) != QUANTILES:
            raise ValueError("SEVEN_ORDERED_QUANTILES_REQUIRED")
        convert = (lambda x: timestamp(x).timestamp()) if row["question_type"] == "date" else number
        values, actual = [convert(p["value"]) for p in points], convert(row["outcome"])
        if values != sorted(values):
            raise ValueError("CROSSING_QUANTILES")
        losses.append(sum(max(q * (actual - v), (q - 1) * (actual - v))
                          for q, v in zip(QUANTILES, values)) / len(values))
        errors.append(abs(values[3] - actual))
        intervals.append(values[1] <= actual <= values[5])
    return {"n": len(records), "mean_pinball": sum(losses) / len(records),
            "median_mae": sum(errors) / len(records), "p10_p90_coverage": sum(intervals) / len(records),
            "units": "seconds" if records[0]["question_type"] == "date" else records[0]["unit"]}


def evaluate(records, *, as_of):
    records = validate_records(records, as_of=as_of)
    groups = defaultdict(list)
    for row in records:
        # Mixing USD, percentage points and other scales would make pinball
        # and MAE averages meaningless. Keep native-unit numeric groups apart.
        unit = row.get("unit", "") if row["question_type"] in {"numeric", "discrete"} else ""
        if row["question_type"] in {"numeric", "discrete"} and not unit:
            raise ValueError("NUMERIC_UNITS_REQUIRED")
        groups[(row["mode"], row["question_type"], unit)].append(row)
    results = []
    for (mode, kind, unit), rows in sorted(groups.items()):
        metrics = (binary_metrics(binary_pairs(rows)) if kind == "binary" else
                   multiclass_metrics(rows) if kind == "multiple_choice" else numeric_metrics(rows))
        results.append({"mode": mode, "question_type": kind, "unit": unit, **metrics})
    return {"as_of": as_of, "questions": len(records), "groups": results,
            "peer_score": None, "logarithm": "natural", "log_reporting_floor": 1e-15,
            "limitations": ["Not the Metaculus peer score; community forecasts and tournament weighting are not supplied.",
                            "Timestamp checks cannot prove absence of LLM pretraining contamination.",
                            "Sparse quantile pinball is not a full numerical CDF log score."]}


def temperature_probability(p, temperature):
    p, temperature = probability(p), number(temperature)
    if temperature <= 0:
        raise ValueError("POSITIVE_TEMPERATURE_REQUIRED")
    if p in (0, 1):
        return p
    scaled = (math.log(p) - math.log1p(-p)) / temperature
    return 1 / (1 + math.exp(-scaled)) if scaled >= 0 else math.exp(scaled) / (1 + math.exp(scaled))


def select_temperature(training, validation, *, as_of):
    """Train once, validate later; return a research artifact, never auto-deploy.

    A separate untouched final test period is still required. Every training
    label must have been known before the first validation forecast.
    """
    validate_records(training + validation, as_of=as_of)
    if len(training) < 100 or len(validation) < 50:
        raise ValueError("MINIMUM_100_TRAIN_50_VALIDATION_REQUIRED")
    modes = {row["mode"] for row in training + validation}
    if len(modes) != 1:
        raise ValueError("MIXED_CALIBRATION_PROVENANCE")
    if max(timestamp(r["resolved_at"]) for r in training) >= min(timestamp(r["forecast_at"]) for r in validation):
        raise ValueError("CALIBRATION_LABEL_LEAKAGE")
    train, valid = binary_pairs(training), binary_pairs(validation)
    candidates = (1.0, .5, .75, 1.25, 1.5, 2.0, 3.0, 4.0)
    transform = lambda pairs, t: [(temperature_probability(p, t), y) for p, y in pairs]
    selected = min(candidates, key=lambda t: binary_metrics(transform(train, t))["log_loss"])
    baseline = binary_metrics(valid)
    candidate = binary_metrics(transform(valid, selected))
    accepted = candidate["log_loss"] < baseline["log_loss"] and candidate["brier"] <= baseline["brier"]
    digest = hashlib.sha256(json.dumps(training + validation, sort_keys=True, allow_nan=False).encode()).hexdigest()
    return {"temperature": selected if accepted else 1.0, "fitted_temperature": selected,
            "validation_improved": accepted, "baseline": baseline, "candidate": candidate,
            "training_questions": len(training), "validation_questions": len(validation),
            "data_sha256": digest, "mode": next(iter(modes)), "as_of": as_of,
            "deployment": "disabled_pending_untouched_test_and_operator_review"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("records", type=Path)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.records.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("EVALUATION_INPUT_TOO_LARGE")
    rows = [json.loads(line) for line in args.records.read_text().splitlines() if line.strip()]
    report = evaluate(rows, as_of=args.as_of)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"questions": report["questions"], "groups": len(report["groups"]), "output": str(args.out)}))


if __name__ == "__main__":
    main()
