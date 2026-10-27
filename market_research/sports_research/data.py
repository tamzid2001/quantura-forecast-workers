"""Strict, source-separated ledger normalization; cashflows retain Decimal precision."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

WITHIN = "p01_hour_within_10c_v1"
OUTSIDE = "p01_hour_outside_10c_opposite_v1"
LIMIT = "games_p01_ask_minus_1c_band_10c_v1"
P99 = "p99_hour_above_opposite_v1"
STRATEGIES = {WITHIN, OUTSIDE, LIMIT, P99}
MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_ROWS = 100_000


def decimal(value, name):
    if isinstance(value, bool) or value is None:
        raise ValueError(f"INVALID_NUMBER:{name}")
    try:
        result = Decimal(str(value))
    except (ValueError, InvalidOperation) as exc:
        raise ValueError(f"INVALID_NUMBER:{name}") from exc
    if not result.is_finite() or abs(result) > Decimal("1e12"):
        raise ValueError(f"INVALID_NUMBER:{name}")
    return result


def timestamp(value):
    if isinstance(value, bool) or value is None or value == "":
        raise ValueError("INVALID_TIMESTAMP")
    try:
        result = float(value)
    except (ValueError, TypeError):
        date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if date.tzinfo is None:
            raise ValueError("TIMESTAMP_REQUIRES_TIMEZONE")
        result = date.timestamp()
    if not math.isfinite(result) or not 0 <= result <= 4_102_444_800:
        raise ValueError("INVALID_TIMESTAMP")
    return result


def iso(at):
    return datetime.fromtimestamp(at, timezone.utc).isoformat()


def day(at):
    return datetime.fromtimestamp(at, timezone.utc).date().isoformat()


def optional_number(row, name):
    value = row.get(name)
    return None if value in (None, "", "None", "null") else float(decimal(value, name))


@dataclass(frozen=True)
class Trade:
    cohort: str
    strategy: str
    fixture: str
    provider: str
    symbol: str
    title: str
    entry_at: float
    settled_at: float | None
    quantity: Decimal
    cost: Decimal
    fee: Decimal
    planned_price: Decimal
    payout: Decimal | None
    features: dict = field(default_factory=dict)

    @property
    def closed(self):
        return self.payout is not None and self.settled_at is not None

    @property
    def spend(self):
        return self.cost + self.fee

    def profit(self, extra_per_contract=0):
        if not self.closed:
            raise ValueError("UNSETTLED_TRADE")
        return self.quantity * self.payout - self.spend - self.quantity * Decimal(str(extra_per_contract))


def normalize(row, cohort):
    strategy = row.get("portfolio", row.get("strategy", LIMIT if cohort == "limit" else ""))
    if strategy not in STRATEGIES or row.get("provider") not in {"kalshi", "polymarket_us"}:
        raise ValueError("UNRECOGNIZED_STRATEGY_OR_PROVIDER")
    fixture = row.get("fixture_id", "")
    if not fixture or len(fixture) > 200:
        raise ValueError("INVALID_FIXTURE")
    quantity = decimal(row["filled"] if cohort == "limit" else row["quantity"], "quantity")
    cost, fee, price = (decimal(row[k], k) for k in ("entry_cost", "entry_fee", "price"))
    if quantity <= 0 or not 0 < cost <= quantity or fee < 0 or not 0 < price < 1:
        raise ValueError("INVALID_ENTRY_CASHFLOW")
    entry = timestamp(row["decision_at"])
    payout_raw = row.get("payout")
    payout = None if payout_raw in (None, "", "None", "null") else decimal(payout_raw, "payout")
    settled_raw = row.get("settlement_at")
    settled = None if settled_raw in (None, "", "None", "null") else timestamp(settled_raw)
    if (payout is None) != (settled is None):
        raise ValueError("PARTIAL_SETTLEMENT_EVIDENCE")
    if payout is not None and (not 0 <= payout <= 1 or settled < entry):
        raise ValueError("INVALID_SETTLEMENT")
    features = {name: optional_number(row, name) for name in (
        "quote_change_std", "quote_change_variance", "quote_change_pairs",
        "feature_cutoff_at", "forecast_iqr", "forecast_tail_width", "discount_quote_sigma",
    )}
    cutoff = features.get("feature_cutoff_at")
    if cutoff is not None and cutoff > min(entry, timestamp(row["signal_at"])):
        raise ValueError("FEATURE_LOOKAHEAD")
    for name in ("quote_change_std", "quote_change_variance", "quote_change_pairs", "forecast_iqr", "forecast_tail_width"):
        if features[name] is not None and features[name] < 0:
            raise ValueError("INVALID_VOLATILITY_FEATURE")
    signal_ask = optional_number(row, "signal_ask")
    if signal_ask is None:
        # Older limit ledgers contain the live order quote, not necessarily the
        # completed-hour signal ask. Do not silently use it as a signal discount.
        discount = optional_number(row, "hourly_discount")
    elif strategy == P99:
        discount = None
    else:
        discount = float(decimal(row["p01"], "p01")) - signal_ask
    features["discount"] = discount
    for name in ("overshoot", "signal_bid_margin", "final_p99", "context_count"):
        features[name] = optional_number(row, name)
    original_ask = optional_number(row, "signal_entry_ask")
    opposite_ask = optional_number(row, "opposite_ask")
    spread = optional_number(row, "entry_spread")
    if spread is None and original_ask is not None and opposite_ask is not None:
        spread = original_ask + opposite_ask - 1
    if spread is not None and not -1e-9 <= spread <= 1:
        raise ValueError("CROSSED_OR_INVALID_ENTRY_SPREAD")
    features["entry_spread"] = None if spread is None else max(0.0, spread)
    features["fee_fraction"] = float(fee / (cost + fee))
    features["planned_price"] = float(price)
    return Trade(cohort, strategy, fixture, row["provider"], row.get("symbol", ""),
                 row.get("event_title", ""), entry, settled, quantity, cost, fee, price, payout, features)


def read_csv(path):
    body = Path(path).read_bytes()
    if not body or len(body) > MAX_INPUT_BYTES:
        raise ValueError("INVALID_LEDGER_SIZE")
    reader = csv.DictReader(io.StringIO(body.decode("utf-8-sig")))
    if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)):
        raise ValueError("INVALID_LEDGER_HEADER")
    result = []
    for row in reader:
        if None in row or len(result) >= MAX_ROWS:
            raise ValueError("INVALID_OR_OVERSIZED_LEDGER")
        result.append(row)
    return result, hashlib.sha256(body).hexdigest()


def load_inputs(directory):
    root = Path(directory)
    trades, sources = [], {}
    paths = {"paper": "comparison-ledger.csv", "limit": "limit-ledger.csv", "month": "ledger.csv"}
    for source, filename in paths.items():
        path = root / source / filename
        artifact_path = root / source / "artifact.json"
        receipt = json.loads(artifact_path.read_text()) if artifact_path.exists() else {}
        if receipt.get("status") == "unavailable":
            sources[source] = {"status": "missing"}
            continue
        if not path.exists():
            sources[source] = {"status": "missing"}
            continue
        rows, sha = read_csv(path)
        stats_path = root / source / ("report.json" if source == "month" else "stats.json")
        meta = json.loads(stats_path.read_text())
        as_of = meta.get("as_of", receipt.get("created_at"))
        if as_of is None and source == "limit":
            as_of = meta.get("equity", {}).get("last_checked_at")
        expected_mode = {"paper": "paper_only", "limit": "paper", "month": "retrospective_paper_only"}[source]
        if meta.get("mode") != expected_mode:
            raise ValueError("INPUT_IS_NOT_THE_EXPECTED_PAPER_MODE")
        receipt_at = receipt.get("created_at", as_of)
        cohort = "historical_verified" if source == "month" else source
        accepted, unfilled = [], 0
        for row in rows:
            if source == "month" and row.get("scope") != "combined_verified_games":
                continue
            if source == "limit" and decimal(row.get("filled", "0"), "filled") == 0:
                unfilled += 1
                continue
            trade = normalize(row, cohort)
            if receipt_at is not None and (trade.entry_at > timestamp(receipt_at)+60
                    or (trade.closed and trade.settled_at > timestamp(receipt_at)+60)):
                raise ValueError("LEDGER_EVENT_AFTER_SOURCE_RECEIPT")
            accepted.append(trade)
        seen = set()
        for trade in accepted:
            key = trade.strategy, trade.fixture
            if key in seen:
                raise ValueError("DUPLICATE_GAME_IN_SOURCE_STRATEGY")
            seen.add(key)
        trades.extend(accepted)
        sources[source] = {"status": "available", "sha256": sha,
                           "as_of": None if as_of is None else iso(timestamp(as_of)),
                           "next_hour": meta.get("next_hour"), "artifact_id": receipt.get("id"),
                           "run_id": receipt.get("workflow_run", {}).get("id"),
                           "entries": len(accepted), "unfilled_intents": unfilled,
                           "study_complete": meta.get("done") if source == "month" else None,
                           "historical_publication": False if source == "month" else None}
    return trades, sources
