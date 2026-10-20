"""Forward-only hourly sports limit worker; paper by default.

Public Actions may run paper mode only. Live mode requires persistent local
disk, explicit approval, exchange credentials, and dedicated venue accounts.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import csv
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import time

from . import game_p01_books as books
from . import game_p01_paper as feed
from .game_p01_limit import (Journal, RULES, TERMINAL, cash_reserve, confirmed, paper_observe, plan, ranked, statistics)
from .game_p01_portfolio import amount, fee

ARTIFACT = "quantura-games-p01-limit-paper-v1"
WORKFLOW = ".github/workflows/games-p01-limit-paper.yml"


def log(event, **values):
    # Live output is aggregate-only. No account identifiers, positions or keys.
    print(json.dumps({"event": event, **values}, sort_keys=True), flush=True)


def restore_paper(journal):
    available = feed.artifacts(ARTIFACT)
    if not available:
        prior = feed.github("actions/workflows/games-p01-limit-paper.yml/runs?status=completed&per_page=1").get("workflow_runs", [])
        if prior:
            raise RuntimeError("MISSING_LIMIT_CHECKPOINT_REQUIRES_REVIEW")
        return
    artifact = available[0]
    feed.validate_run(artifact, WORKFLOW)
    state = feed.compressed_json(feed.github(f"actions/artifacts/{artifact['id']}/zip", True), "limit-state.json.gz")
    if (state.get("schema") != 1 or state.get("mode") != "paper" or state.get("account") != "public"
            or state.get("rules_hash") != RULES.fingerprint):
        raise ValueError("INVALID_LIMIT_PAPER_CHECKPOINT")
    statistics(state, {}, time.time())
    journal.state = state
    journal.save()


def source_rows(hour):
    catalog = {key: feed.artifacts("quantura-public-"+key) for key in feed.FEEDS}
    return feed.snapshots_at(hour, catalog)[0]


def signals(rows, hour, registry, venues):
    mapping = feed.identities(rows, registry)
    selected, counts = [], Counter()
    for row in rows:
        if row["provider"] not in venues:
            continue
        predictions = row.get("predictions", [])
        prediction = next((p for p in predictions if feed.stamp(p["timestamp"]) == hour), None)
        if (not prediction or not feed.model_verified(row)
                or not feed.stamp(row["game_start"]) < hour <= feed.stamp(row["forecast_end"])
                or amount(predictions[-1]["quantiles"]["0.01"]) <= amount(".5")):
            continue
        fixture = mapping.get(row["provider"]+":"+str(row["event_id"]))
        if not fixture:
            counts["unverified_game_identity"] += 1
            continue
        selected.append({**{key: row[key] for key in ("provider", "symbol", "side", "contract_id", "event_id", "event_title", "artifact_id")},
                         "fixture_id": fixture, "identity_verified": True, "seven_model_verified": True,
                         "forecast_id": row["id"], "p01": prediction["quantiles"]["0.01"],
                         "final_p01": predictions[-1]["quantiles"]["0.01"], "signal_at": hour,
                         "game_start": feed.stamp(row["game_start"]), "forecast_end": feed.stamp(row["forecast_end"]),
                         "published_at": row["publication_ts"],
                         "forecast_sha256": hashlib.sha256(json.dumps(predictions, sort_keys=True).encode()).hexdigest()})
    return selected, counts


def prepare(row):
    try:
        hour = row["signal_at"]
        if time.time() > hour+RULES.max_signal_age:
            raise ValueError("MISSED_LIVE_DECISION_WINDOW")
        # The candle only establishes the completed-hour signal; it never fills
        # an order or supplies the current order limit.
        samples = [p for p in feed.prices(row, hour-60, hour) if hour-60 < p["at"] <= hour]
        if not samples or not 0 < amount(samples[-1]["ask"]) < amount(row["p01"]):
            raise ValueError("NO_COMPLETED_HOUR_BELOW_P01_SIGNAL")
        metadata = books.constraints(row)
        row = {**row, "signal_ask": samples[-1]["ask"], "signal_quote_at": samples[-1]["at"],
               "fee_coefficient": metadata["fee_coefficient"]}
        return {"candidate": row, "metadata": metadata}, None
    except (RuntimeError, ValueError, KeyError, TypeError) as error:
        return None, str(error) if isinstance(error, (RuntimeError, ValueError)) else "INCOMPLETE_PUBLIC_MARKET_DATA"


def current_plan(value):
    try:
        current = books.book(value["candidate"])
        return plan(value["candidate"], current, value["metadata"], time.time()), None
    except (RuntimeError, ValueError, KeyError, TypeError) as error:
        return None, str(error) if isinstance(error, (RuntimeError, ValueError)) else "INCOMPLETE_PUBLIC_BOOK"


class Worker:
    def __init__(self, journal, venues, brokers=None):
        self.journal, self.venues, self.brokers = journal, venues, brokers or {}
        self.stop = False
        self.last_settlement = self.last_stats = self.last_prepare = 0
        self.prepared_hour, self.rows = None, None

    def enter_hour(self, hour):
        state = self.journal.state
        if hour <= state["activation_at"]:
            return
        rows = self.rows if self.prepared_hour == hour else source_rows(hour)
        candidates, counts = signals(rows, hour, state["identities"], self.venues)
        candidates = [r for r in candidates if not self.journal.blocked(r["fixture_id"])]
        with ThreadPoolExecutor(max_workers=4) as pool:
            prepared = list(pool.map(prepare, candidates))
            for _, error in prepared:
                if error:
                    counts[error] += 1
            planned = list(pool.map(current_plan, [value for value, _ in prepared if value]))
        for _, error in planned:
            if error:
                counts[error] += 1
        for order in ranked([value for value, _ in planned if value]):
            if self.journal.blocked(order["fixture_id"]):
                continue
            try:
                # Recheck the clock and BBO immediately before durable intent.
                order = plan(order, books.book(order), order["metadata"], time.time())
                if state["mode"] == "paper":
                    from .game_p01_limit import trades
                    filled = trades(state)
                    spent = sum((amount(t["entry_cost"])+amount(t["entry_fee"])
                                 -amount(t.get("payout") or 0)*amount(t["quantity"]) for t in filled), amount(0))
                    reserved = sum((cash_reserve(o) for o in state["orders"]), amount(0))
                    cash = amount(state["initial_cash"])-spent-reserved
                else:
                    from .game_p01_live import account_clean
                    if state.get("entry_halt"):
                        raise RuntimeError("LIVE_ENTRY_HALT_REQUIRES_REVIEW")
                    broker = self.brokers[order["provider"]]
                    account_clean(broker, state["orders"])
                    cash = broker.balance(order)
                # Split partial executions can each round a fee. Reserve a
                # conservative extra cent for every possible quantity step.
                if cash < cash_reserve(order):
                    raise ValueError("INSUFFICIENT_CASH_FOR_NOTIONAL_AND_FEES")
                if time.time() > order["signal_at"]+RULES.max_signal_age or time.time()-order["quote_at"] > RULES.max_book_age:
                    raise ValueError("DECISION_EXPIRED_DURING_READINESS")
                if not self.journal.reserve(order):
                    continue
                stored = state["orders"][-1]
                if state["mode"] == "paper":
                    stored["state"] = "resting"
                else:
                    stored["state"] = "sending"
                    self.journal.save()
                    try:
                        stored["order_id"] = self.brokers[order["provider"]].submit(stored)
                        stored["state"] = "resting"
                    except (RuntimeError, ValueError):
                        stored["state"] = "unknown"
                        state["entry_halt"] = "UNCERTAIN_CREATE_ORDER_NO_AUTOMATIC_RESUBMIT"
                        self.journal.save()
                        raise RuntimeError("UNCERTAIN_CREATE_ORDER_NO_AUTOMATIC_RESUBMIT") from None
                self.journal.save()
                committed = sum((cash_reserve(o)+(amount(o["entry_cost"])+amount(o["entry_fee"])
                                if o.get("payout") is None else amount(0)) for o in state["orders"]), amount(0))
                state["peak_cash_committed_and_reserved_usd"] = max(
                    state.get("peak_cash_committed_and_reserved_usd", 0), float(committed))
                self.journal.save()
                counts["orders_prepared"] += 1
            except (RuntimeError, ValueError) as error:
                counts[str(error)] += 1
        state["coverage"][str(hour)] = dict(counts)
        state["last_hour"] = hour
        self.journal.save()
        log("sports_limit_hour", mode=state["mode"], hour=hour, coverage=dict(counts))

    def reconcile(self, now):
        state, marks = self.journal.state, {}
        settlement_due = now-self.last_settlement >= 60
        for order in state["orders"]:
            if order.get("payout") is not None:
                continue
            try:
                if order["state"] not in TERMINAL:
                    if state["mode"] == "paper":
                        if now >= order["expires_at"]:
                            order["state"] = "expired"
                        else:
                            paper_observe(order, books.book(order), time.time())
                    else:
                        broker = self.brokers[order["provider"]]
                        receipt = broker.reconcile(order)
                        confirmed(order, receipt, time.time())
                        self.journal.save()
                        if time.time() >= order["expires_at"] and order["state"] not in TERMINAL:
                            broker.cancel(order)
                            # Cancel acknowledgments do not exclude racing fills.
                            confirmed(order, broker.reconcile(order), time.time())
                if amount(order["filled"]) > 0:
                    if settlement_due and order["state"] in TERMINAL:
                        payout = books.payout(order)
                        if payout is not None and (state["mode"] == "paper" or self.brokers[order["provider"]].settled(order, payout)):
                            order.update(payout=str(payout), settlement_at=int(time.time()),
                                         settlement_time_basis="first_confirmed_receipt_not_assumed_game_end")
                    if order.get("payout") is None:
                        current = books.book(order)
                        if current["bids"] and 0 <= time.time()-current["received_at"] <= RULES.max_book_age:
                            marks[order["forecast_id"]] = {"bid": max(amount(p) for p, _ in current["bids"]),
                                                           "at": current["received_at"]}
                self.journal.save()
            except (RuntimeError, ValueError, KeyError) as error:
                name = str(error) if isinstance(error, (RuntimeError, ValueError)) else "INCOMPLETE_RECEIPT"
                state.setdefault("errors", {})[name] = state.get("errors", {}).get(name, 0)+1
                if state["mode"] == "live":
                    # Continue reconciliation, but never admit more risk while
                    # accounting or a cancel/create delivery remains uncertain.
                    state["entry_halt"] = name
                self.journal.save()
        if settlement_due:
            self.last_settlement = now
        statistics(state, marks, time.time())
        self.journal.save()
        return marks

    def report(self, directory, marks):
        value = statistics(self.journal.state, marks, time.time())
        value["entry_halt"] = self.journal.state.get("entry_halt")
        log("sports_limit_stats", **value)
        self.journal.checkpoint(directory)
        Path(directory, "stats.json").write_text(json.dumps(value, indent=2)+"\n")
        fields = ["fixture_id", "provider", "symbol", "side", "signal_at", "decision_at", "quote_ask", "p01", "price",
                  "quantity", "filled", "entry_cost", "entry_fee", "state", "expires_at", "payout", "settlement_at", "accounting"]
        with Path(directory, "limit-ledger.csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(self.journal.state["orders"])

    def run(self, directory, minutes):
        deadline = time.monotonic()+minutes*60
        marks = {}
        try:
            while not self.stop and time.monotonic() < deadline:
                now = time.time()
                marks = self.reconcile(now)
                hour = int(now//3600*3600)
                # Missed hours are coverage gaps, never historical live entries.
                if self.journal.state["last_hour"] < hour:
                    if 2 <= now-hour <= RULES.max_signal_age:
                        try:
                            self.enter_hour(hour)
                        except (RuntimeError, ValueError, KeyError):
                            log("sports_limit_source_retry", hour=hour)
                    elif now-hour > RULES.max_signal_age:
                        self.journal.state["coverage"][str(hour)] = {"missed_forward_decision_window": 1}
                        self.journal.state["last_hour"] = hour
                        self.journal.save()
                # Cache published forecasts before the next hour; only market
                # observations after that hour can trigger an order.
                if now-hour >= 3300 and self.prepared_hour != hour+3600 and now-self.last_prepare >= 60:
                    self.last_prepare = now
                    try:
                        self.rows = source_rows(hour+3600)
                        self.prepared_hour = hour+3600
                    except (RuntimeError, ValueError, KeyError):
                        log("sports_limit_prefetch_retry")
                if now-self.last_stats >= 60:
                    self.report(directory, marks)
                    self.last_stats = now
                time.sleep(5)
        finally:
            if self.journal.state["mode"] == "live":
                for order in self.journal.state["orders"]:
                    if order.get("order_id") and order["state"] not in TERMINAL:
                        try:
                            broker = self.brokers[order["provider"]]
                            broker.cancel(order)
                            confirmed(order, broker.reconcile(order), time.time())
                        except (RuntimeError, ValueError, KeyError):
                            self.journal.state["entry_halt"] = "SHUTDOWN_CANCEL_UNCONFIRMED_NATIVE_EXPIRY_REMAINS"
            self.report(directory, marks)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("paper", "live"), default="paper")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--minutes", type=int, default=330)
    parser.add_argument("--venues", default="kalshi,polymarket_us")
    parser.add_argument("--subaccount", type=int, default=0)
    parser.add_argument("--readiness", action="store_true")
    parser.add_argument("--clear-halt", action="store_true", help="After authenticated reconciliation, clear the entry halt (readiness only)")
    parser.add_argument("--resolve-order", nargs=2, metavar=("CLIENT_ID", "EXCHANGE_ORDER_ID"),
                        help="Adopt a manually identified uncertain order after authenticated validation (readiness only)")
    parser.add_argument("--restore-artifact", action="store_true")
    args = parser.parse_args()
    venues = set(args.venues.split(","))
    if not venues or not venues <= {"kalshi", "polymarket_us"} or not 0 <= args.minutes <= 525600:
        parser.error("Invalid venue or duration")
    if (args.clear_halt or args.resolve_order) and not args.readiness:
        parser.error("Order recovery requires --readiness; it cannot submit orders")
    brokers, account = {}, "public"
    if args.mode == "live":
        if os.environ.get("GITHUB_ACTIONS") == "true" or args.restore_artifact:
            raise RuntimeError("LIVE_REQUIRES_PERSISTENT_HOST_NO_ARTIFACT_RESTORE")
        from .game_p01_live import Broker, account_clean, approval, code_hash, live_authorized
        for venue in sorted(venues):
            brokers[venue] = Broker(venue, subaccount=args.subaccount)
        account = hashlib.sha256(json.dumps({k: b.identity for k, b in brokers.items()}, sort_keys=True).encode()).hexdigest()
        authorized = live_authorized(venues, args.subaccount, account)
        if not args.readiness and not authorized:
            raise RuntimeError("SPORTS_LIVE_APPROVAL_REQUIRED_RUN_READINESS")
        for broker in brokers.values():
            broker.enabled = authorized and not args.readiness
    journal = Journal(args.output/"limit-journal.sqlite", args.mode, account, now=int(time.time()))
    try:
        if args.readiness:
            if args.mode != "live":
                raise ValueError("READINESS_REQUIRES_LIVE_MODE_NO_ORDER_WRITES")
            if args.resolve_order:
                client_id, exchange_id = args.resolve_order
                if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", exchange_id):
                    raise ValueError("INVALID_EXCHANGE_ORDER_ID")
                matches = [o for o in journal.state["orders"] if o["client_order_id"] == client_id and not o.get("order_id")]
                if len(matches) != 1:
                    raise ValueError("UNCERTAIN_LOCAL_ORDER_NOT_UNIQUE")
                recovered = {**matches[0], "order_id": exchange_id, "fills": list(matches[0]["fills"])}
                receipt = brokers[recovered["provider"]].reconcile(recovered)
                confirmed(recovered, receipt, time.time())
                matches[0].update(recovered)
                journal.save()
            for order in journal.state["orders"]:
                if order["state"] not in TERMINAL:
                    receipt = brokers[order["provider"]].reconcile(order)
                    confirmed(order, receipt, time.time())
                    journal.save()
            for broker in brokers.values():
                account_clean(broker, journal.state["orders"])
                if broker.balance() <= 0:
                    raise RuntimeError("ACCOUNT_HAS_NO_AVAILABLE_FUNDS")
            if args.clear_halt:
                journal.state.pop("entry_halt", None)
                journal.save()
            log("sports_limit_readiness", authenticated=True, live_orders_enabled=False,
                approval_rules=approval(venues, args.subaccount), approval_code=code_hash(), approval_account=account,
                venues=sorted(venues), persistent_journal=str(journal.path))
            return
        if args.restore_artifact:
            if journal.state["orders"] or journal.state["activation_at"] < time.time()-120:
                raise RuntimeError("ARTIFACT_RESTORE_REQUIRES_NEW_EPHEMERAL_PAPER_JOURNAL")
            restore_paper(journal)
        worker = Worker(journal, venues, brokers)
        def stop(_signum, _frame):
            worker.stop = True
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        worker.run(args.output, args.minutes)
    finally:
        journal.close()
        for broker in brokers.values():
            broker.close()


if __name__ == "__main__":
    main()
