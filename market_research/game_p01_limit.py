"""Shared forward-only sports limit rule and durable order accounting.

Paper book crossings are explicitly proxies. Only the live broker adapter can
produce authenticated cumulative fills. A submitted order is never retried as
a new order after uncertain delivery.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_DOWN
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid

from .game_p01_portfolio import amount, fee, performance, sample_equity


@dataclass(frozen=True)
class LimitRules:
    name: str = "games_p01_ask_minus_1c_band_10c_v1"
    notional: str = "1.00"
    offset: str = "0.01"
    band: str = "0.10"
    max_signal_age: int = 120
    max_book_age: int = 5
    minimum_latency: int = 1

    @property
    def fingerprint(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


RULES = LimitRules()
TERMINAL = frozenset({"filled", "canceled", "expired", "rejected"})


def fresh_book(book, now):
    # REST receipt age and request duration bound transport freshness; a book's
    # last change may legitimately be old. Neither is an exchange fill receipt.
    return (0 <= now-book["received_at"] <= RULES.max_book_age
            and 0 <= book["received_at"]-book["requested_at"] <= RULES.max_book_age
            and book.get("open") is True
            and (book.get("source_at") is None or book["source_at"] <= now+1))


def plan(candidate, book, metadata, now):
    """Reject stale signals; never lift the price to fit the P01 band/tick."""
    c = candidate
    if ((c.get("provider"), c.get("side")) not in {
            ("kalshi", "yes"), ("kalshi", "no"), ("polymarket_us", "long"), ("polymarket_us", "short")}):
        raise ValueError("INVALID_ECONOMIC_SIDE")
    if not (c.get("identity_verified") and c.get("fixture_id") and c.get("seven_model_verified")
            and Decimal(".5") < amount(c["final_p01"]) <= 1
            and c["published_at"] <= c["signal_at"] < now <= c["signal_at"]+RULES.max_signal_age
            and c["game_start"] < c["signal_at"] <= c["forecast_end"]
            and c["signal_at"]-60 < c["signal_quote_at"] <= c["signal_at"]
            and 0 < amount(c["signal_ask"]) < amount(c["p01"]) <= 1
            and metadata["market_close"] > now and fresh_book(book, now)):
        raise ValueError("INELIGIBLE_OR_STALE_SIGNAL")
    asks, bids = book["asks"], book["bids"]
    if not asks or not bids:
        raise ValueError("TWO_SIDED_BOOK_REQUIRED")
    if any(not 0 <= amount(p) <= 1 or amount(q) <= 0 for p, q in asks+bids):
        raise ValueError("INVALID_DISPLAYED_DEPTH")
    ask, bid = min(amount(p) for p, _ in asks), max(amount(p) for p, _ in bids)
    p01 = amount(c["p01"])
    if not 0 <= bid <= ask < p01:
        raise ValueError("CURRENT_ASK_NOT_BELOW_P01")
    price = ask-amount(RULES.offset)
    if not 0 < price < 1 or not p01-amount(RULES.band) <= price < p01:
        raise ValueError("LIMIT_OUTSIDE_P01_BAND")
    # Kalshi's tick ranges apply on the YES book even for economic NO buys.
    yes_price = price if c["side"] in ("yes", "long") else 1-price
    if c["provider"] == "kalshi" and yes_price != yes_price.quantize(Decimal(".0001")):
        raise ValueError("UNSUPPORTED_EXCHANGE_PRICE_PRECISION")
    ranges = metadata["price_ranges"]
    if not any(amount(r["start"]) <= yes_price <= amount(r["end"])
               and amount(r["step"]) > 0
               and (yes_price-amount(r["start"])) % amount(r["step"]) == 0 for r in ranges):
        raise ValueError("EXACT_DISCOUNT_NOT_ON_EXCHANGE_TICK")
    step = amount(metadata["quantity_step"])
    minimum = amount(metadata["minimum_quantity"])
    if not 0 < step <= minimum:
        raise ValueError("INVALID_QUANTITY_RULES")
    quantity = (amount(RULES.notional)/price/step).to_integral_value(rounding=ROUND_DOWN)*step
    if quantity < minimum:
        raise ValueError("NOTIONAL_BELOW_MARKET_MINIMUM")
    expiry = min(c["signal_at"]+3600, int(metadata["market_close"]))
    if expiry <= now+RULES.minimum_latency:
        raise ValueError("ENTRY_WINDOW_CLOSED")
    identity = f"{RULES.fingerprint}:{c['fixture_id']}:{c['signal_at']}:{c['provider']}:{c['contract_id']}"
    return {**c, "price": str(price), "quantity": str(quantity), "quantity_step": str(step),
            "decision_at": now, "quote_at": book["received_at"], "quote_ask": str(ask),
            "entry_spread": str(ask-bid),
            "expires_at": expiry, "client_order_id": str(uuid.uuid5(uuid.NAMESPACE_URL, identity)),
            "rules_hash": RULES.fingerprint, "metadata": metadata,
            "filled": "0", "entry_cost": "0", "entry_fee": "0", "fills": [],
            "state": "prepared", "order_id": None}


def ranked(plans):
    # Same-hour ranking uses only known prices and estimated costs, no outcomes.
    return sorted(plans, key=lambda p: (-(amount(p["p01"])-amount(p["price"])
                   -fee(p["provider"], 1, p["price"], p.get("fee_coefficient"))),
                   p["provider"], p["contract_id"]))


def cash_reserve(order):
    left = amount(order["quantity"])-amount(order["filled"])
    if left <= 0 or order["state"] in TERMINAL:
        return amount(0)
    return (left*amount(order["price"])
            + fee(order["provider"], left, order["price"], order["fee_coefficient"])
            + amount(".01")*left/amount(order["quantity_step"]))


class Journal:
    """One persistent disk, one process, FULL synchronous SQLite intent commits.

    Live mode binds the journal to a credential fingerprint. It must run on a
    persistent host, never an ephemeral Actions runner with artifact recovery.
    """
    def __init__(self, path, mode, account="public", cash="100", now=0):
        if mode not in ("paper", "live"):
            raise ValueError("INVALID_TRADING_MODE")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = self.path.with_suffix(".lock").open("a+")
        os.chmod(self.lock.name, 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise RuntimeError("PERSISTENT_JOURNAL_ALREADY_OWNED") from None
        self.db = sqlite3.connect(self.path)
        os.chmod(self.path, 0o600)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS checkpoint (id INTEGER PRIMARY KEY CHECK(id=1), state TEXT NOT NULL)")
        row = self.db.execute("SELECT state FROM checkpoint WHERE id=1").fetchone()
        self.state = json.loads(row[0]) if row else {
            "schema": 1, "mode": mode, "account": account, "rules_hash": RULES.fingerprint,
            "activation_at": now, "initial_cash": str(amount(cash)), "orders": [],
            "identities": {}, "coverage": {}, "equity": {}, "last_hour": now//3600*3600}
        if (self.state["mode"] != mode or self.state["account"] != account
                or self.state["rules_hash"] != RULES.fingerprint):
            self.close()
            raise ValueError("JOURNAL_MODE_ACCOUNT_OR_RULES_MISMATCH")
        self.save()

    def save(self):
        raw = json.dumps(self.state, sort_keys=True, allow_nan=False)
        with self.db:
            self.db.execute("INSERT INTO checkpoint VALUES(1,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state", (raw,))

    def close(self):
        self.db.close()
        self.lock.close()

    def reserve(self, order):
        if self.blocked(order["fixture_id"]):
            return False
        if any(o["client_order_id"] == order["client_order_id"] for o in self.state["orders"]):
            return False
        self.state["orders"].append({**order, "mode": self.state["mode"]})
        self.save()  # MUST commit before the first network write.
        return True

    def blocked(self, fixture):
        return any(o["fixture_id"] == fixture and (amount(o["filled"]) > 0 or o["state"] not in TERMINAL)
                   for o in self.state["orders"])

    def checkpoint(self, directory):
        self.save()
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        temp = directory/"limit-state.tmp"
        temp.write_bytes(gzip.compress(json.dumps(self.state, sort_keys=True).encode(), mtime=0))
        temp.replace(directory/"limit-state.json.gz")


def paper_observe(order, book, now):
    """Strict-through, visible-depth-limited proxy; touches never become fills.

    Each distinct displayed book can contribute liquidity once. This omits
    queue-priority fills and can undercount fills; it still cannot prove fills.
    """
    if order["state"] in TERMINAL:
        return
    if now >= order["expires_at"]:
        order["state"] = "expired"
        return
    if (now < order["decision_at"]+RULES.minimum_latency or not fresh_book(book, now)
            or book["requested_at"] < order["decision_at"]+RULES.minimum_latency):
        return
    if not book["asks"] or not book["bids"]:
        return
    if "paper_accepted_at" not in order:
        # A post-only order already marketable on the first arrival sample
        # might be rejected. Do not award that crossing as a convenient fill.
        if min(amount(p) for p, _ in book["asks"]) <= amount(order["price"]):
            order.update(state="rejected", rejection_basis="post_only_marketable_at_first_arrival_sample")
            return
        order["paper_accepted_at"] = now
        return
    fingerprint = hashlib.sha256(json.dumps([book["asks"], book["bids"]], sort_keys=True).encode()).hexdigest()
    if fingerprint in order.setdefault("seen_books", []):
        return
    order["seen_books"].append(fingerprint)
    order["book_samples"] = order.get("book_samples", 0)+1
    limit = amount(order["price"])
    if any(amount(p) == limit for p, _ in book["asks"]):
        order["touch_samples"] = order.get("touch_samples", 0)+1
    consumed = order.setdefault("consumed_depth", {})
    available = sorted([(amount(p), max(amount(0), amount(q)-amount(consumed.get(str(amount(p)), 0))))
                        for p, q in book["asks"] if 0 < amount(p) < limit])
    visible = sum((q for _, q in available), Decimal(0))
    left = amount(order["quantity"])-amount(order["filled"])
    step = amount(order["quantity_step"])
    quantity = (min(left, visible)/step).to_integral_value(rounding=ROUND_DOWN)*step
    if quantity <= 0:
        return
    allocation = quantity
    for observed_price, depth in available:
        taken = min(allocation, depth)
        key = str(observed_price)
        consumed[key] = str(amount(consumed.get(key, 0))+taken)
        allocation -= taken
        if allocation == 0:
            break
    # Charge the fixed resting limit (no favorable price improvement) and the
    # validated taker fee, even when a real passive fill might earn lower fees.
    cost = quantity*limit
    paid_fee = fee(order["provider"], quantity, limit, order.get("fee_coefficient"))
    order["fills"].append({"id": fingerprint, "at": now, "quantity": str(quantity),
                           "price": str(limit), "fee": str(paid_fee), "observed_book": book,
                           "visible_strictly_below_limit": str(visible)})
    order["filled"] = str(amount(order["filled"])+quantity)
    order["entry_cost"] = str(amount(order["entry_cost"])+cost)
    order["entry_fee"] = str(amount(order["entry_fee"])+paid_fee)
    order.setdefault("first_fill_at", now)
    order["state"] = "filled" if amount(order["filled"]) == amount(order["quantity"]) else "resting"
    order["accounting"] = "strict_through_sampled_book_depth_proxy_not_confirmed_fill"


def confirmed(order, receipt, now):
    """Apply authenticated cumulative quantities/costs/fees without double debit."""
    if receipt["order_id"] != order["order_id"]:
        raise ValueError("ORDER_RECEIPT_IDENTITY_MISMATCH")
    quantity, cost, paid_fee = map(amount, (receipt["filled"], receipt["cost"], receipt["fees"]))
    if not (amount(order["filled"]) <= quantity <= amount(order["quantity"])
            and amount(order["entry_cost"]) <= cost <= quantity*amount(order["price"])
            and paid_fee >= amount(order["entry_fee"]) and receipt["state"] in TERMINAL | {"resting"}
            and (cost > 0 if quantity > 0 else cost == paid_fee == 0)
            and (receipt["state"] != "filled" or quantity == amount(order["quantity"]))):
        raise ValueError("INVALID_CUMULATIVE_FILL_RECEIPT")
    if quantity:
        first = float(receipt["first_fill_at"])
        if not order["decision_at"] <= first <= now:
            raise ValueError("INVALID_FILL_TIMESTAMP")
    if quantity > amount(order["filled"]) or cost != amount(order["entry_cost"]) or paid_fee != amount(order["entry_fee"]):
        order["fills"].append({"at": now, "quantity": str(quantity-amount(order["filled"])),
                               "cost": str(cost-amount(order["entry_cost"])),
                               "fee": str(paid_fee-amount(order["entry_fee"])),
                               "time_basis": "authenticated_cumulative_receipt_confirmation"})
    order.update(filled=str(quantity), entry_cost=str(cost), entry_fee=str(paid_fee),
                 state=receipt["state"], accounting="authenticated_exchange_cumulative_fills_and_fees")
    if quantity:
        order.setdefault("first_fill_at", first)


def trades(state):
    result = []
    for order in state["orders"]:
        quantity = amount(order["filled"])
        if quantity <= 0:
            continue
        row = {**order, "quantity": str(quantity), "price": str(amount(order["entry_cost"])/quantity),
               "decision_at": order["first_fill_at"]}
        result.append(row)
    return result


def statistics(state, marks, now):
    filled = trades(state)
    previous = state["equity"].get("last_complete_at")
    state["equity"] = sample_equity(filled, marks, now, state["equity"])
    if previous is not None and state["equity"].get("last_complete_at") == now:
        state["equity"]["max_complete_sample_gap_seconds"] = max(
            state["equity"].get("max_complete_sample_gap_seconds", 0), now-previous)
    orders = state["orders"]
    result = performance(filled)
    # Fund fills when observed, including multiple partial fills, rather than
    # debiting the entire basket at its first entry timestamp.
    cashflows = []
    for o in orders:
        for f in o["fills"]:
            cost = amount(f["cost"]) if "cost" in f else amount(f["quantity"])*amount(f["price"])
            cashflows.append((f["at"], 0, -(cost+amount(f["fee"]))))
        if o.get("payout") is not None:
            cashflows.append((o["settlement_at"], 1, amount(o["payout"])*amount(o["filled"])))
    cash = minimum = amount(0)
    for _, _, delta in sorted(cashflows):
        cash += delta
        minimum = min(minimum, cash)
    result["historical_minimum_cash_usd"] = float(-minimum)
    result["capital_basis"] = "observed_partial_fill_and_confirmed_settlement_cashflows_excludes_pending_order_reserves_not_future_sufficiency"
    return {"strategy": RULES.name, "rules_hash": RULES.fingerprint, "mode": state["mode"],
            "activation_at": state["activation_at"], **result, "equity": state["equity"],
            "submitted_orders": len(orders), "pending_orders": sum(o["state"] not in TERMINAL for o in orders),
            "pending_cash_reserve_usd": float(sum((cash_reserve(o) for o in orders), amount(0))),
            "peak_cash_committed_and_reserved_usd": state.get("peak_cash_committed_and_reserved_usd", 0),
            "unfilled_terminal_orders": sum(o["state"] in TERMINAL and amount(o["filled"]) == 0 for o in orders),
            "partial_orders": sum(0 < amount(o["filled"]) < amount(o["quantity"]) for o in orders),
            "uncertain_deliveries": sum(o["state"] in ("prepared", "sending", "unknown") for o in orders),
            "paper_fill_basis": "strict_through_sampled_book_depth_proxy_no_queue_fill_claim" if state["mode"] == "paper" else None,
            "drawdown_basis": "sampled_bid_equity_with_coverage_not_continuous", "coverage": state["coverage"]}
