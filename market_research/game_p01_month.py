"""Resumable retrospective sports study; public GETs and local artifacts only.

Reconstructs the last eligible pregame forecast from genuine completed hours.
This is NOT an archive of forecasts published before the historical games.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, time as dt_time, timedelta
import csv
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
import signal
import time
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from .engine import digest, iso, stamp
from .game_p01_paper import KALSHI, POLY, candle_price, identities, public_json
from .game_p01_hourly_comparison import HOURLY_RULES, fill_hourly, select_hourly
from .game_p01_portfolio import amount, performance

NY = ZoneInfo("America/New_York")
SCHEMA = "quantura-sports-month-v1"
MODELS = ("prophet", "toto", "granite", "chronos", "timesfm", "timestral", "t0")
QUANTILES = (.01, .25, .5, .75, .9, .99)
SHARDS = 4


def runtime_fingerprint():
    root = Path(__file__).resolve().parents[1]
    files = [Path(__file__), root/"market_research/forecast.py", root/"market_research/pregame_screener.py",
             root/"market_research/game_p01_hourly_comparison.py", root/"market_research/game_p01_portfolio.py",
             root/"quantura_site/functions_explore/src/ensembleModelRegistry.json"]
    files += sorted((root/"ensemble_forecasting").rglob("*.py"))
    files = [p for p in files if "tests" not in p.parts]
    return digest({str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files})


def emit(name, **fields):
    print(json.dumps({"event": name, "mode": "retrospective_paper_only", **fields}, sort_keys=True), flush=True)


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix+".tmp")
    with gzip.open(temporary, "wt", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, allow_nan=False)
    temporary.replace(path)


def load(path):
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        value = json.load(stream)
    if value.get("schema") != SCHEMA:
        raise ValueError("UNRECOGNIZED_MONTH_CHECKPOINT")
    return value


def bounds(first, last):
    a, b = date.fromisoformat(first), date.fromisoformat(last)
    if a > b or (b-a).days > 365:
        raise ValueError("INVALID_STUDY_DATES")
    return (int(datetime.combine(a, dt_time(), NY).timestamp()),
            int(datetime.combine(b+timedelta(days=1), dt_time(), NY).timestamp()))


def game_series(ticker, title=""):
    return bool(re.search(r"(?:GAME|MATCH|MONEYLINE)$", ticker)
                and not re.search(r"1H|2H|F3|F5|QUARTER|PERIOD|SPREAD|TOTAL|HALF|RFI|BTTS|1STHOME|FIRSTHOME|OPPONENT", ticker)
                and not re.search(r"first opponent|who (?:will|is).{0,30}play|opponent at", title, re.I))


def moneyline(value):
    return bool(re.search(r"^(SPORTS_MARKET_TYPE_(MONEYLINE|DRAWABLE_OUTCOME)|moneyline)$|_full_game_(moneyline|winner)$", value, re.I))


def pregame_cutoff(start):
    # The screener stops at the START of the kickoff hour, including 17:30 starts.
    return (int(start)//3600-1)*3600


def native_key(row):
    return row["provider"]+":"+row["contract_id"]


def basic(provider, event_id, title, league, market, side, contract, start, schedule):
    symbol = str(market.get("ticker") if provider == "kalshi" else market.get("slug"))
    created = market.get("open_time") if provider == "kalshi" else market.get("createdAt")
    close = market.get("close_time") if provider == "kalshi" else market.get("endDate")
    return {"provider": provider, "event_id": str(event_id), "event_title": title,
            "league": league, "symbol": symbol, "side": side, "contract_id": str(contract),
            "game_start": iso(start), "cutoff": pregame_cutoff(start),
            "created_at": stamp(created) if created else None,
            "market_close": stamp(close) if close else None,
            "schedule_evidence": schedule, "metadata": {k: market[k] for k in (
                "status", "result", "settlement_value_dollars", "settlement_ts", "fractional_trading_enabled",
                "minimumTradeQty", "feeCoefficient", "ticker", "slug") if k in market}}


def kalshi_rows(event, milestones, series, start, end):
    event_id = event["event_ticker"]
    linked = [m for m in milestones if event_id in [*m.get("primary_event_tickers", []), *m.get("related_event_tickers", [])]]
    starts = {stamp(m["start_date"]) for m in linked if m.get("start_date")}
    if len(starts) != 1:
        return [], "missing_or_conflicting_linked_milestone"
    kickoff = starts.pop()
    if not start <= kickoff < end:
        return [], "outside_dates"
    evidence = {"type": "linked_sports_milestone", "start": iso(kickoff),
                "ids": [m.get("id") for m in linked]}
    rows = []
    for market in event.get("markets", []):
        if market.get("market_type") != "binary" or market.get("event_ticker") != event_id:
            continue
        for side in ("yes", "no"):
            row = basic("kalshi", event_id, event["title"], series["ticker"], market, side,
                        market["ticker"]+":"+side, kickoff, evidence)
            row["series_fee"] = {k: series.get(k) for k in ("fee_type", "fee_multiplier")}
            row["event_fee_multiplier"] = event.get("fee_multiplier")
            rows.append(row)
    return rows, "accepted"


def poly_rows(event, start, end):
    rows, skips = [], Counter()
    # Date filters are an acceleration only. Validate each market's actual kickoff.
    for market in event.get("markets", []):
        if not moneyline(str(market.get("sportsMarketTypeV2") or market.get("sportsMarketType") or market.get("marketType", ""))):
            skips["not_full_game_winner"] += 1
            continue
        raw_start = market.get("gameStartTime") or event.get("startTime")
        if not raw_start:
            skips["missing_game_start"] += 1
            continue
        kickoff = stamp(raw_start)
        if not start <= kickoff < end:
            skips["outside_dates"] += 1
            continue
        for side in market.get("marketSides", []):
            if type(side.get("long")) is not bool or not side.get("id"):
                continue
            row = basic("polymarket_us", event["id"], event.get("title", ""), event.get("seriesSlug", ""),
                        market, "long" if side["long"] else "short", side["id"], kickoff,
                        {"type": "market_gameStartTime" if market.get("gameStartTime") else "event_startTime", "start": iso(kickoff)})
            rows.append(row)
    return rows, skips


def request(path, provider, receipts):
    root = KALSHI if provider == "kalshi" else POLY
    data = public_json(root+path)
    receipts.append({"url": root+path, "fetched_at": iso(int(time.time())), "sha256": digest(data)})
    time.sleep(.10)  # Public requests stay well below the documented per-IP limits.
    return data


def catalog(directory, first, last, seconds=1200):
    directory = Path(directory)
    path = directory/"catalog.json.gz"
    start, end = bounds(first, last)
    state = load(path) if path.exists() else {
        "schema": SCHEMA, "first": first, "last": last, "start": start, "end": end,
        "contracts": {}, "receipts": [], "counts": {}, "done": False,
        "poly_offset": 0, "poly_done": False, "kalshi_index": 0, "kalshi_cursor": "", "kalshi_done": False}
    if (state["first"], state["last"]) != (first, last):
        raise ValueError("STUDY_DATES_CHANGED")
    fingerprint = runtime_fingerprint()
    if state.get("runtime_fingerprint", fingerprint) != fingerprint:
        raise ValueError("RESEARCH_RUNTIME_CHANGED")
    state["runtime_fingerprint"] = fingerprint
    deadline = time.monotonic()+seconds
    counts = Counter(state["counts"])
    try:
        if "series" not in state:
            data = request("/series?category=Sports", "kalshi", state["receipts"])
            state["series"] = sorted([s for s in data.get("series", []) if game_series(s["ticker"], s.get("title", ""))], key=lambda s: s["ticker"])
        while not state["poly_done"] and time.monotonic() < deadline:
            params = {"limit": 100, "offset": state["poly_offset"], "orderBy": "id", "orderDirection": "desc", "categories": "sports",
                      "startTimeMin": iso(start), "startTimeMax": iso(end-1)}
            data = request("/v1/events?"+urlencode(params), "polymarket_us", state["receipts"])
            events = data.get("events")
            if not isinstance(events, list):
                raise ValueError("INVALID_POLY_EVENT_PAGE")
            ids = [str(e["id"]) for e in events]
            if ids and digest(ids) in state.setdefault("poly_pages", []):
                raise ValueError("REPEATED_POLY_CATALOG_PAGE")
            state.setdefault("poly_pages", []).append(digest(ids))
            for e in events:
                rows, skipped = poly_rows(e, start, end)
                counts.update({"poly_"+k: v for k, v in skipped.items()})
                for row in rows:
                    state["contracts"][native_key(row)] = row
            counts["poly_events"] += len(events)
            state["poly_offset"] += len(events)
            state["poly_done"] = len(events) < 100
            save(path, {**state, "counts": dict(counts)})
            emit("month_catalog", provider="polymarket_us", events=counts["poly_events"], outcomes=len(state["contracts"]))
        while not state["kalshi_done"] and time.monotonic() < deadline:
            if state["kalshi_index"] >= len(state["series"]):
                state["kalshi_done"] = True
                break
            series = state["series"][state["kalshi_index"]]
            params = {"series_ticker": series["ticker"], "min_close_ts": start, "limit": 200,
                      "with_nested_markets": "true", "with_milestones": "true"}
            if state["kalshi_cursor"]:
                params["cursor"] = state["kalshi_cursor"]
            data = state.get("kalshi_pending")
            if data is None:
                data = request("/events?"+urlencode(params), "kalshi", state["receipts"])
                state["kalshi_pending"] = data
                state["kalshi_pending_index"] = 0
            events = data.get("events")
            if not isinstance(events, list):
                raise ValueError("INVALID_KALSHI_EVENT_PAGE")
            for index in range(state["kalshi_pending_index"], len(events)):
                if time.monotonic() >= deadline:
                    break
                e = events[index]
                # Event metadata survives market archival. Retrieve archived markets
                # only for events whose verified kickoff is in the study window.
                linked = [m for m in data.get("milestones", []) if e["event_ticker"] in
                          [*m.get("primary_event_tickers", []), *m.get("related_event_tickers", [])]]
                starts = {stamp(m["start_date"]) for m in linked if m.get("start_date")}
                if len(starts) == 1 and start <= next(iter(starts)) < end:
                    archived = request("/historical/markets?"+urlencode({"event_ticker": e["event_ticker"], "limit": 1000}),
                                       "kalshi", state["receipts"])
                    if archived.get("cursor"):
                        raise ValueError("INCOMPLETE_ARCHIVED_EVENT_MARKETS")
                    union = {m["ticker"]: m for m in [*e.get("markets", []), *archived.get("markets", [])]}
                    e = {**e, "markets": list(union.values())}
                rows, reason = kalshi_rows(e, data.get("milestones", []), series, start, end)
                counts["kalshi_"+reason] += 1
                for row in rows:
                    state["contracts"][native_key(row)] = row
                counts["kalshi_events"] += 1
                state["kalshi_pending_index"] = index+1
            if state["kalshi_pending_index"] < len(events):
                break
            cursor = data.get("cursor", "")
            if cursor and cursor == state["kalshi_cursor"]:
                raise ValueError("REPEATED_KALSHI_CURSOR")
            state["kalshi_cursor"] = cursor
            if not cursor:
                state["kalshi_index"] += 1
            state.pop("kalshi_pending", None)
            state.pop("kalshi_pending_index", None)
            save(path, {**state, "counts": dict(counts)})
            emit("month_catalog", provider="kalshi", series_done=state["kalshi_index"], series_total=len(state["series"]), outcomes=len(state["contracts"]))
        state["done"] = state["poly_done"] and state["kalshi_done"]
        # Resolve duplicate-game identities only against the fully frozen universe.
        if state["done"]:
            registry = {}
            by_day = defaultdict(list)
            for row in state["contracts"].values():
                by_day[datetime.fromtimestamp(stamp(row["game_start"]), NY).date()].append(row)
            for rows in by_day.values():
                identities(rows, registry)
            state["identities"] = registry
            state["catalog_hash"] = digest(state["contracts"])
    finally:
        state["counts"] = dict(counts)
        save(path, state)
    return state


def parse_points(data, provider, start, end):
    """Keep native observations; no trade-price substitution or gap filling."""
    points = {}
    if provider == "kalshi":
        candles = data.get("candlesticks", [])
        for c in candles:
            at = int(c["end_period_ts"])
            bid, ask = candle_price(c.get("yes_bid", {})), candle_price(c.get("yes_ask", {}))
            if start < at <= end and bid is not None and ask is not None and 0 <= bid <= ask <= 1:
                points[at] = {"at": at, "ask": ask, "bid": bid}
    else:
        for p in data.get("history", []):
            at = int(p["timestamp"])
            yes, no = p.get("longPrice"), p.get("shortPrice")
            if yes is None or no is None:
                continue
            ask, bid = float(amount(yes)), float(1-amount(no))
            if start < at <= end and 0 <= bid <= ask <= 1:
                points[at] = {"at": at, "ask": ask, "bid": bid}
    return sorted(points.values(), key=lambda p: p["at"])


def side_points(points, side):
    if side in ("yes", "long"):
        return points
    return [{"at": p["at"], "ask": float(1-amount(p["bid"])), "bid": float(1-amount(p["ask"]))} for p in points]


def completed_hours(points, cutoff, provider):
    result = {}
    for point in sorted(points, key=lambda p: p["at"]):
        # Native Kalshi candles are end stamped; Poly raw observations start buckets.
        close = point["at"] if provider == "kalshi" else (point["at"]//3600+1)*3600
        if close <= cutoff:
            result[close] = {**point, "at": close}
    return list(result.values())


def cached_history(row, directory, interval, start, end, deadline):
    provider, symbol = row["provider"], row["symbol"]
    points = {}
    chunk = 2000*3600 if provider == "kalshi" and interval == 60 else 86400
    for left in range(int(start), int(end), chunk):
        if time.monotonic() >= deadline:
            raise TimeoutError("CHECKPOINT_TIME_BUDGET")
        right = min(end, left+chunk)
        key = digest([provider, symbol, interval, left, right])
        path = Path(directory)/"history"/(key+".json.gz")
        if path.exists():
            receipt = load(path)
        else:
            if provider == "kalshi":
                params = urlencode({"start_ts": left, "end_ts": right, "period_interval": interval})
                # Archives are the primary source for completed games. The live series tier
                # is only a fallback when the archive has not migrated this market yet.
                url = KALSHI+"/historical/markets/"+quote(symbol, safe="")+"/candlesticks?"+params
                data = public_json(url, True)
                if not data or not data.get("candlesticks"):
                    url = KALSHI+"/series/"+quote(symbol.split("-", 1)[0], safe="")+"/markets/"+quote(symbol, safe="")+"/candlesticks?"+params
                    data = public_json(url, True) or {}
            else:
                url = POLY+"/v1/price-history?"+urlencode({"symbol": symbol, "timestamp.startTimestamp": left,
                    "timestamp.endTimestamp": right, "fidelity": 1})
                data = public_json(url)
            receipt = {"schema": SCHEMA, "url": url, "fetched_at": iso(int(time.time())),
                       "raw_sha256": digest(data), "points": parse_points(data, provider, left, right)}
            save(path, receipt)
            time.sleep(.10)
        for p in receipt["points"]:
            points[p["at"]] = p
    return sorted(points.values(), key=lambda p: p["at"])


def settlement(row):
    m = row["metadata"]
    if row["provider"] == "kalshi":
        if m.get("status") not in ("settled", "finalized"):
            return None
        value = m.get("settlement_value_dollars")
        if value is None:
            value = {"yes": 1, "no": 0}.get(m.get("result"))
        if value is None or not 0 <= amount(value) <= 1:
            return None
        if m.get("result") in ("yes", "no") and amount(value) != (1 if m["result"] == "yes" else 0):
            raise ValueError("CONFLICTING_SETTLEMENT")
        payout = float(amount(value))
        return {"affirmative_payout": payout, "settlement_at": stamp(m["settlement_ts"]) if m.get("settlement_ts") else None,
                "evidence": "official_Kalshi_settlement_value", "checked_at": int(time.time())}
    if m.get("status") != "MARKET_STATUS_RESOLVED":
        return None
    data = public_json(POLY+"/v1/markets/"+quote(row["symbol"], safe="")+"/settlement")
    value = data.get("settlement")
    if data.get("slug") != row["symbol"] or type(value) not in (int, float) or not 0 <= value <= 1:
        raise ValueError("UNVERIFIED_POLY_SETTLEMENT")
    return {"affirmative_payout": value, "settlement_at": None,
            "evidence": "official_Polymarket_US_settlement_no_credit_timestamp", "checked_at": int(time.time())}


def quantity_fees(row):
    m = row["metadata"]
    if row["provider"] == "kalshi":
        f = row["series_fee"]
        if (f["fee_type"] not in ("quadratic", "quadratic_with_maker_fees")
                or amount(f.get("fee_multiplier") or 1) != 1
                or amount(row.get("event_fee_multiplier") or 1) != 1):
            raise ValueError("UNSUPPORTED_FEE_SCHEDULE")
        return (".01" if m.get("fractional_trading_enabled") is True else "1"), ".07"
    step, coefficient = m.get("minimumTradeQty"), m.get("feeCoefficient")
    if step is None or coefficient is None:
        raise ValueError("MISSING_QUANTITY_OR_FEE_TERMS")
    if amount(step) not in (amount(".01"), amount(1)):
        raise ValueError("UNSUPPORTED_QUANTITY_RULES")
    return str(step), str(coefficient)


def reconstruct(row, directory, deadline):
    """A single frozen pregame origin suffices for all subsequent in-game hours."""
    from .engine import Quote
    from .forecast import forecast_window
    from .pregame_screener import game_models, through_deadline
    cutoff, kickoff = row["cutoff"], stamp(row["game_start"])
    if row["created_at"] is None:
        raise ValueError("MISSING_LISTING_TIME")
    if row["created_at"] > cutoff-31*3600:
        raise ValueError("INSUFFICIENT_POSSIBLE_HOURLY_HISTORY")
    step, coefficient = quantity_fees(row)
    # Collect from listing, even when older than the 500 observations used by inference.
    raw = cached_history(row, directory, 60, row["created_at"], cutoff, deadline)
    hourly = completed_hours(side_points(raw, row["side"]), cutoff, row["provider"])
    context = hourly[-500:]
    models = game_models(len(context))
    end = kickoff+4*3600
    horizon = math.ceil((end-context[-1]["at"])/3600)
    if not 1 <= horizon <= 512:
        raise ValueError("HISTORY_TOO_STALE")
    quotes = [Quote(p["at"], p["ask"], p["bid"]) for p in context]
    seconds_left = min(3300, deadline-time.monotonic())
    if seconds_left < 60:
        raise TimeoutError("CHECKPOINT_TIME_BUDGET")
    def interrupt(_signum, _frame):
        raise TimeoutError("MODEL_TIME_BUDGET")
    previous = signal.signal(signal.SIGALRM, interrupt)
    signal.setitimer(signal.ITIMER_REAL, seconds_left)
    try:
        forecast = forecast_window(quotes, horizon, models=models, quantiles=QUANTILES,
                                   frequency="1h", weight_mode="auto", failure_policy="fail")
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    if ({m["id"] for m in forecast["models"] if m["status"] == "completed"} != set(MODELS)
            or forecast.get("weighting", {}).get("policy") != "rolling_loss_selection_v2"):
        raise ValueError("STRICT_SEVEN_MODEL_POLICY_REQUIRED")
    predictions = through_deadline(forecast["rows"], forecast["origin"], context[-1]["ask"], end)
    result = {"forecast": forecast, "predictions": predictions, "hourly_download_count": len(hourly),
              "context_count": len(context), "cutoff": cutoff, "kickoff": kickoff, "end": end,
              "quantity_step": step, "fee_coefficient": coefficient,
              "publication_assumption": "available_at_start_of_kickoff_hour_after_previous_hour_cutoff",
              "assumed_available_at": cutoff+3600,
              "historical_publication": False}
    return result


def candidate_rows(row, result, directory, deadline):
    candidates, coverage = [], Counter()
    if result["predictions"][-1]["quantiles"]["0.01"] <= .5:
        return [], {"final_p01_not_above_half": 1}
    # Do not use the synthetic final partial-hour model point as a completed-hour signal.
    hours = [p for p in result["predictions"] if result["kickoff"] < p["timestamp"] <= result["end"]
             and p["timestamp"] % 3600 == 0 and not p.get("interpolated_model_point")]
    if not hours:
        return [], {"no_live_prediction_hours": 1}
    points = side_points(cached_history(row, directory, 1, hours[0]["timestamp"]-60,
                                       hours[-1]["timestamp"]+60, deadline), row["side"])
    for prediction in hours:
        hour, p01 = prediction["timestamp"], prediction["quantiles"]["0.01"]
        observed = [p for p in points if hour-60 < p["at"] <= hour]
        follow = [p for p in points if hour < p["at"] <= hour+60 and 0 < p["ask"] < min(1, p01)]
        if not observed or not 0 < observed[-1]["ask"] < p01:
            coverage["no_below_p01_hour_quote"] += 1
            continue
        if not follow:
            coverage["missing_qualifying_next_minute_ask"] += 1
            continue
        point = follow[0]
        candidates.append({"forecast_id": result["forecast"]["forecast_id"],
            "provider": row["provider"], "symbol": row["symbol"], "side": row["side"],
            "contract_id": row["contract_id"], "event_id": row["event_id"], "event_title": row["event_title"],
            "fixture_id": row.get("fixture_id"), "identity_verified": bool(row.get("fixture_id")),
            "seven_model_verified": True, "forecast_sha256": digest(result["predictions"]),
            "price": point["ask"], "opposite_ask": str(1-amount(point["bid"])),
            "signal_ask": observed[-1]["ask"], "signal_quote_at": observed[-1]["at"],
            "p01": p01, "final_p01": result["predictions"][-1]["quantiles"]["0.01"],
            "signal_at": hour, "decision_at": point["at"], "quote_at": point["at"],
            "published_at": result["assumed_available_at"], "historical_publication": False,
            "game_start": result["kickoff"], "forecast_end": result["end"], "market_close": row["market_close"],
            "quantity_step": result["quantity_step"], "minimum_quantity": result["quantity_step"],
            "fee_coefficient": result["fee_coefficient"], "outcome_evidence": result["settlement"]})
    return candidates, dict(coverage)


PERMANENT = {"MISSING_LISTING_TIME", "INSUFFICIENT_POSSIBLE_HOURLY_HISTORY", "INSUFFICIENT_HISTORY_FOR_SEVEN_MODELS",
             "HISTORY_TOO_STALE", "UNSUPPORTED_FEE_SCHEDULE", "MISSING_QUANTITY_OR_FEE_TERMS", "UNSUPPORTED_QUANTITY_RULES"}


def replay(directory, shard, seconds=13200):
    directory = Path(directory)
    catalog_state = load(directory/"catalog.json.gz")
    if catalog_state["runtime_fingerprint"] != runtime_fingerprint():
        raise ValueError("RESEARCH_RUNTIME_CHANGED")
    state_path = directory/f"shard-{shard}.json.gz"
    state = load(state_path) if state_path.exists() else {"schema": SCHEMA, "shard": shard, "items": {}}
    if not catalog_state["done"]:
        save(state_path, state)
        return state
    if state.get("catalog_hash", catalog_state["catalog_hash"]) != catalog_state["catalog_hash"]:
        raise ValueError("FROZEN_CATALOG_CHANGED")
    state["catalog_hash"] = catalog_state["catalog_hash"]
    deadline = time.monotonic()+seconds
    tasks = sorted((r for k, r in catalog_state["contracts"].items()
                    if int(hashlib.sha256(k.encode()).hexdigest(), 16) % SHARDS == shard), key=native_key)
    for original in tasks:
        key = native_key(original)
        item = state["items"].get(key, {})
        if item.get("status") in ("complete", "excluded"):
            continue
        if time.monotonic() >= deadline-120:
            break
        row = {**original, "fixture_id": catalog_state.get("identities", {}).get(original["provider"]+":"+original["event_id"])}
        try:
            # Persist expensive inference before requesting execution history.
            result = item.get("result") or reconstruct(row, directory, deadline)
            item.update(result=result, status="forecasted")
            state["items"][key] = item
            save(state_path, state)
            if "settlement" not in result:
                result["settlement"] = settlement(row)
                save(state_path, state)
            candidates, coverage = candidate_rows(row, result, directory, deadline)
            item.update(status="complete", candidates=candidates, coverage=coverage)
            item.pop("error", None)
            emit("month_outcome_complete", provider=row["provider"], symbol=row["symbol"], side=row["side"],
                 context=result["context_count"], available_hours=result["hourly_download_count"], candidates=len(candidates))
        except (RuntimeError, ValueError, TimeoutError, KeyError, ArithmeticError) as error:
            code = str(error) if re.fullmatch(r"[A-Z0-9_:]{1,100}", str(error)) else type(error).__name__
            item.update(status="excluded" if code in PERMANENT else item.get("status", "retry"), error=code,
                        attempts=item.get("attempts", 0)+1)
            state["items"][key] = item
            emit("month_outcome_pending", provider=row["provider"], symbol=row["symbol"], side=row["side"], code=code)
        finally:
            save(state_path, state)
    state["task_count"] = len(tasks)
    state["finished_count"] = sum(v.get("status") in ("complete", "excluded") for v in state["items"].values())
    state["done"] = state["finished_count"] == len(tasks)
    save(state_path, state)
    return state


def summarize(trades):
    """Do not invent Polymarket credit times to compute portfolio funding or DD."""
    settled = [t for t in trades if t.get("payout") is not None]
    synthetic = []
    for t in sorted(settled, key=lambda x: (x["decision_at"], x["fixture_id"])):
        synthetic.append({**t, "settlement_at": t["decision_at"]})
    ordered = performance(synthetic)
    all_timed = all(t.get("settlement_at") is not None for t in settled)
    confirmed = performance(trades) if all_timed else None
    return {"entries": len(trades), "settled": len(settled), "open": len(trades)-len(settled),
            **{k: ordered[k] for k in ("wins", "losses", "breakevens", "win_rate_pct", "net_pnl_usd", "gross_pnl_usd",
                                      "return_on_closed_spend_pct", "max_win_streak", "max_loss_streak", "avg_entry_cents")},
            "fees_all_entries_usd": float(sum((amount(t["entry_fee"]) for t in trades), amount(0))),
            "streak_basis": "entry_order_of_officially_settled_games_not_account_credit_order",
            "entry_order_outcome_drawdown_usd": ordered["max_realized_drawdown_usd"],
            "max_realized_drawdown_usd": confirmed["max_realized_drawdown_usd"] if confirmed else None,
            "recorded_minimum_cash_usd": confirmed["historical_minimum_cash_usd"] if confirmed else None,
            "fund_all_entries_without_reusing_payouts_usd": float(sum((amount(t["entry_cost"])+amount(t["entry_fee"]) for t in trades), amount(0))),
            "settlement_credit_timestamps_complete": all_timed,
            "sampled_equity_drawdown_usd": None,
            "capital_note": "No verified minimum deposit without all settlement credit times; full cash funding is a non-recycling bound, not a recommended deposit."}


def report(directory):
    directory = Path(directory)
    cat = load(directory/"catalog.json.gz")
    shards = [load(directory/f"shard-{i}.json.gz") if (directory/f"shard-{i}.json.gz").exists()
              else {"items": {}, "done": False} for i in range(SHARDS)]
    items = {k: v for s in shards for k, v in s["items"].items()}
    candidates = [c for v in items.values() for c in v.get("candidates", [])]
    result = {"schema": SCHEMA, "as_of": iso(int(time.time())), "first": cat["first"], "last": cat["last"],
              "mode": "retrospective_paper_only", "historical_publication": False,
              "catalog_done": cat["done"], "outcomes": len(cat["contracts"]),
              "catalog_counts": cat["counts"], "statuses": dict(Counter(v["status"] for v in items.values())),
              "errors": dict(Counter(v["error"] for v in items.values() if v.get("error"))),
              "done": cat["done"] and all(s.get("done") for s in shards), "portfolios": {}}
    result["progress"] = cat["poly_offset"]+cat["kalshi_index"]*1000000+sum(
        v.get("status") in ("complete", "excluded") for v in items.values())
    ledger = []
    for scope in ("combined_verified_games", "kalshi", "polymarket_us"):
        pool = []
        for c in candidates:
            if scope == "combined_verified_games":
                if c.get("identity_verified"):
                    pool.append(c)
            elif c["provider"] == scope:
                pool.append({**c, "fixture_id": scope+":"+c["event_id"], "identity_verified": True})
        result["portfolios"][scope] = {}
        for rule in HOURLY_RULES:
            selected, skips = select_hourly(pool, (), rule, cat["start"])
            trades = []
            for candidate in selected:
                t = fill_hourly(candidate, rule)
                evidence = candidate.get("outcome_evidence")
                if evidence:
                    payout = evidence["affirmative_payout"]
                    t["payout"] = payout if t["side"] in ("yes", "long") else float(1-amount(payout))
                    t["settlement_at"] = evidence["settlement_at"]
                trades.append(t)
                ledger.append({"scope": scope, "strategy": rule.name, **t})
            result["portfolios"][scope][rule.name] = {**summarize(trades), "selection_skips": skips}
    (directory/"report.json").write_text(json.dumps(result, indent=2, sort_keys=True)+"\n")
    fields = sorted({k for t in ledger for k in t})
    with (directory/"ledger.csv").open("w") as f:
        writer = csv.DictWriter(f, fields)
        writer.writeheader()
        writer.writerows([{k: json.dumps(v, sort_keys=True) if isinstance(v, (dict, list)) else v for k, v in t.items()} for t in ledger])
    lines = ["# Past-month sports P01 study", "", f"{cat['first']}–{cat['last']} (America/New_York). Complete: **{result['done']}**.", "",
        "Retrospective regenerated forecasts, not forecasts published before these games. Seven production models, automatic rolling loss selection and six quantiles. All available pregame hourly history is collected; the latest 500 genuine completed observations enter inference. Missing hours remain missing.", "",
        "The final eligible pregame cutoff is one hour before the kickoff hour. Earlier refreshes are unnecessary for the in-game-only signals because the final pregame forecast replaces them. Availability is assumed at the start of the kickoff hour; historical scheduler latency and revised kickoff schedules cannot be verified.", "",
        "Each rule buys at the next recorded minute ask, $1 principal plus modeled fees, and holds to official settlement. The opposite ask is 1 minus the original bid. These are quote proxies without historical depth, not passive −1c order fills. Fee/quantity terms are current metadata, not a verified historical fee archive.", "",
        "Combined results admit only unambiguous cross-venue game identities; venue-only results include native games. Each portfolio holds at most one position per game. Venue-only returns overlap and must not be added together.", "",
        "| Scope / rule | Settled W/L | Win rate | Net after fees | Entry-order outcome DD |", "| --- | ---: | ---: | ---: | ---: |"]
    for scope, portfolios in result["portfolios"].items():
        for rule in HOURLY_RULES:
            s = portfolios[rule.name]
            win = "N/A" if s["win_rate_pct"] is None else f"{s['win_rate_pct']:.2f}%"
            lines.append(f"| {scope} / {rule.label} | {s['wins']}/{s['losses']} | {win} | ${s['net_pnl_usd']:.4f} | ${s['entry_order_outcome_drawdown_usd']:.4f} |")
    lines += ["", "Entry-order outcome drawdown/streaks describe settled outcomes in entry order, not simultaneous account equity. Unknown Polymarket settlement credit times prevent verified minimum-deposit and chronological portfolio-DD estimates. Minute book history cannot establish continuous equity drawdown.", "",
              f"Catalog outcomes: {result['outcomes']}. Statuses: {result['statuses']}. Errors/exclusions: {result['errors']}.", "",
              "An incomplete run is a coverage report, not a full-month performance claim. Missing data or failed models never become invented trades or reduced-model forecasts.", ""]
    (directory/"REPORT.md").write_text("\n".join(lines))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("catalog", "replay", "report"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--first", default="2026-09-20")
    parser.add_argument("--last", default="2026-10-19")
    parser.add_argument("--shard", type=int, choices=range(SHARDS), default=0)
    parser.add_argument("--seconds", type=int)
    args = parser.parse_args()
    if args.command == "catalog":
        value = catalog(args.directory, args.first, args.last, args.seconds or 1200)
    elif args.command == "replay":
        value = replay(args.directory, args.shard, args.seconds or 13200)
    else:
        value = report(args.directory)
    emit("month_progress", command=args.command, done=bool(value.get("done")),
         items=len(value.get("items", value.get("contracts", {}))))


if __name__ == "__main__":
    main()
