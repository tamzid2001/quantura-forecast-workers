"""Read-only, checkpointed P01 game quote replay. No broker or database writes.

The scheduler observes completed hours, then replays the first recorded minute
ask below P01. These are delayed quote proxies, not executable fill receipts.
Only exact participant matches confirmed across both venues are admitted;
ambiguous identities are skipped rather than permitting duplicate exposure.
"""
from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import time
import unicodedata
from urllib.error import HTTPError
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen
import zipfile

from .game_p01_portfolio import BASELINE, SHALLOW, amount, fill, performance, sample_equity, select

REPO = "tamzid2001/quantura-forecast-workers"
ARTIFACT = "quantura-games-p01-paper-v1"
WORKFLOW = ".github/workflows/games-p01-paper.yml"
MODELS = {"prophet", "toto", "granite", "chronos", "timesfm", "timestral", "t0"}
FEEDS = [f"games-{provider}-{shard}" for provider in ("kalshi", "polymarket_us") for shard in (0, 1)]
KALSHI = "https://external-api.kalshi.com/trade-api/v2"
POLY = "https://gateway.polymarket.us"
MAX_BYTES = 32*1024*1024
try:
    import certifi
    TLS_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    TLS_CONTEXT = ssl.create_default_context()


def stamp(value):
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def event(name, **values):
    print(json.dumps({"event": name, "mode": "paper_only", **values}, sort_keys=True), flush=True)


def github(path, binary=False):
    # gh owns GitHub authentication and signed download redirects; provider
    # requests below never receive GITHUB_TOKEN or any exchange credential.
    result = subprocess.run(["gh", "api", f"repos/{REPO}/{path}"], capture_output=True, timeout=90, check=False)
    if result.returncode:
        raise RuntimeError("GITHUB_READ_FAILED:"+path.split("?")[0])
    if len(result.stdout) > MAX_BYTES:
        raise ValueError("ARTIFACT_TOO_LARGE")
    return result.stdout if binary else json.loads(result.stdout)


def public_json(url, missing_ok=False):
    if not url.startswith((KALSHI+"/", POLY+"/")):
        raise ValueError("UNAPPROVED_DATA_HOST")
    for attempt in range(3):
        try:
            request = Request(url, headers={"User-Agent": "Quantura-Paper-Research/1.0", "Accept": "application/json"})
            with urlopen(request, timeout=25, context=TLS_CONTEXT) as response:
                if response.url.split("/")[2] != url.split("/")[2]:
                    raise ValueError("PROVIDER_HOST_REDIRECT")
                body = response.read(MAX_BYTES+1)
                if len(body) > MAX_BYTES:
                    raise ValueError("PROVIDER_RESPONSE_TOO_LARGE")
                return json.loads(body)
        except HTTPError as error:
            if missing_ok and error.code == 404:
                return None
            if error.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise RuntimeError(f"PUBLIC_DATA_HTTP_{error.code}") from None
        except (TimeoutError, OSError):
            if attempt == 2:
                raise RuntimeError("PUBLIC_DATA_UNAVAILABLE") from None
        time.sleep(attempt+1)


def compressed_json(blob, member):
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        members = archive.infolist()
        if len(members) != 1 or members[0].filename != member or members[0].file_size > MAX_BYTES:
            raise ValueError("INVALID_ARTIFACT_MEMBERS")
        packed = archive.read(member)
    with gzip.GzipFile(fileobj=io.BytesIO(packed)) as stream:
        raw = stream.read(MAX_BYTES+1)
    if len(raw) > MAX_BYTES:
        raise ValueError("DECOMPRESSED_ARTIFACT_TOO_LARGE")
    return json.loads(raw)


def artifacts(name):
    values = []
    for page in (1, 2, 3):
        result = github("actions/artifacts?"+urlencode({"name": name, "per_page": 100, "page": page}))
        batch = result.get("artifacts", [])
        values += [a for a in batch if not a.get("expired") and a.get("name") == name
                   and a.get("workflow_run", {}).get("head_branch") == "main"]
        if len(batch) < 100:
            break
    return sorted(values, key=lambda a: (a["created_at"], a["id"]), reverse=True)


def validate_run(artifact, workflow):
    run = github(f"actions/runs/{artifact['workflow_run']['id']}")
    if (run.get("path") != workflow or run.get("head_branch") != "main"
            or run.get("repository", {}).get("full_name") != REPO
            or run.get("event") not in ("schedule", "workflow_dispatch")):
        raise ValueError("UNTRUSTED_ARTIFACT_PROVENANCE")


def fresh_state(now):
    return {"schema": 1, "mode": "paper_only", "activation_at": now,
            "next_hour": (now//3600+1)*3600, "identities": {}, "coverage": {},
            "portfolios": {r.name: {"rules_hash": r.fingerprint(), "trades": [], "equity": {}}
                           for r in (BASELINE, SHALLOW)}}


def validate_state(state):
    if state.get("schema") != 1 or state.get("mode") != "paper_only":
        raise ValueError("INVALID_PAPER_CHECKPOINT")
    for rule in (BASELINE, SHALLOW):
        portfolio = state["portfolios"][rule.name]
        if portfolio["rules_hash"] != rule.fingerprint():
            raise ValueError("PAPER_RULES_CHANGED_NEW_PORTFOLIO_REQUIRED")
        performance(portfolio["trades"])
        if any(t["mode"] != "paper_only" or t["rules_hash"] != rule.fingerprint() for t in portfolio["trades"]):
            raise ValueError("INVALID_PAPER_LEDGER")
    return state


def restore(now):
    available = artifacts(ARTIFACT)
    if not available:
        activated = os.environ.get("GAMES_P01_PAPER_ACTIVATED_AT")
        if activated and now-stamp(activated) > 3600:
            raise RuntimeError("MISSING_CUMULATIVE_CHECKPOINT_REQUIRES_REVIEW")
        event("paper_initialized", activation_at=iso(now), first_signal_at=iso((now//3600+1)*3600))
        return fresh_state(now)
    newest = available[0]
    validate_run(newest, WORKFLOW)
    state = validate_state(compressed_json(github(f"actions/artifacts/{newest['id']}/zip", True), "paper-state.json.gz"))
    event("paper_checkpoint_restored", artifact_id=newest["id"], next_hour=iso(state["next_hour"]))
    return state


def save(state, directory):
    validate_state(state)
    directory.mkdir(parents=True, exist_ok=True)
    temp = directory/"paper-state.tmp"
    temp.write_bytes(gzip.compress(json.dumps(state, sort_keys=True).encode(), mtime=0))
    temp.replace(directory/"paper-state.json.gz")


def normalized(value):
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    value = re.sub(r"\b(esports?|e sports)\b", "", value)
    return re.sub(r"[^a-z0-9]", "", value)


# Reviewed spelling equivalences; never fuzzy-match unrelated teams or surnames.
ALIASES = {
    "carhurricanes": "carolinahurricanes", "carolina": "carolinahurricanes",
    "cgyflames": "calgaryflames", "calgary": "calgaryflames",
    "columbus": "columbusbluejackets", "cbjbluejackets": "columbusbluejackets",
    "minnesota": "minnesotawild", "minwild": "minnesotawild",
    "sportivoluqueno": "luqueno", "sportivosanlorenzo": "sanlorenzo",
    "llanerosfc": "llaneros", "independientesantafe": "independsantafe",
    "cabelgrano": "belgranodecordoba", "cariverplate": "riverplate",
    "adcali": "cali", "cdpjuniorfc": "junior",
}


def participants(row):
    title = str(row.get("event_title", "")).split(":", 1)[0]
    sides = re.split(r"\s+vs\.?\s+", title, flags=re.IGNORECASE)
    if len(sides) != 2:
        return None
    keys = sorted(ALIASES.get(normalized(s), normalized(s)) for s in sides)
    return tuple(keys) if all(keys) and keys[0] != keys[1] else None


def identities(rows, registry):
    """Only admit uniquely matched cross-venue fixtures, then pin native IDs.

    Props with the same participant pair share a fixture. Rematches more than
    three hours apart or conflicting prior identities are deliberately excluded.
    """
    groups = defaultdict(list)
    for row in rows:
        pair = participants(row)
        if pair:
            groups[pair].append(row)
    # Tennis sources often publish surnames versus full player names. Accept
    # only a unique two-player suffix match in the same singles tour and time
    # window; doubles and ambiguous rematches remain excluded.
    tennis = [r for r in rows if (str(r.get("league", "")) in ("ATP", "WTA")
              or re.fullmatch(r"KX(ATP|WTA)(MATCH|EXACTMATCH)", str(r.get("league", "")))) and participants(r)]
    aliases = {}
    for pair, values in list(groups.items()):
        tours = {"ATP" if "ATP" in str(v.get("league", "")) else "WTA" for v in values}
        if not any(v in tennis for v in values) or len(tours) != 1:
            continue
        partners = set()
        for candidate_pair, others in groups.items():
            if candidate_pair == pair or not any(v in tennis for v in others):
                continue
            if not any(v["provider"] != w["provider"] and abs(stamp(v["game_start"])-stamp(w["game_start"])) <= 3*3600
                       and ("ATP" in str(v.get("league", ""))) == ("ATP" in str(w.get("league", "")))
                       for v in values for w in others):
                continue
            if any(all(len(a) >= 4 and len(b) >= 4 and (a.endswith(b) or b.endswith(a)) for a, b in zip(pair, ordered))
                   for ordered in (candidate_pair, candidate_pair[::-1])):
                partners.add(candidate_pair)
        if len(partners) == 1:
            aliases[pair] = next(iter(partners))
    for pair, partner in aliases.items():
        if aliases.get(partner) == pair and pair in groups and partner in groups:
            canonical = max((pair, partner), key=lambda p: (sum(map(len, p)), p))
            other = partner if canonical == pair else pair
            groups[canonical] += groups.pop(other)
    result = {}
    for pair, values in groups.items():
        starts = [stamp(v["game_start"]) for v in values]
        if max(starts)-min(starts) > 3*3600:
            continue
        native = {v["provider"]+":"+str(v["event_id"]) for v in values}
        pinned = {registry[n] for n in native if n in registry}
        if len(pinned) > 1:
            continue
        if not pinned and {v["provider"] for v in values} != {"kalshi", "polymarket_us"}:
            continue
        fixture = next(iter(pinned)) if pinned else "game:"+hashlib.sha256(
            json.dumps([pair, min(starts)//86400], sort_keys=True).encode()).hexdigest()[:24]
        for native_id in native:
            registry[native_id] = fixture
            result[native_id] = fixture
    # A pinned native ID remains valid if its other venue disappears from later snapshots.
    return {**registry, **result}


def model_verified(row):
    return (row.get("schema_version") == 3 and set(row.get("models", [])) == MODELS
            and row.get("weight_mode") == "auto" and row.get("weight_policy") == "rolling_loss_selection_v2"
            and not row.get("recomputed_at"))


def snapshots_at(hour, catalog):
    rows, proof = [], {}
    for feed in FEEDS:
        before = [a for a in catalog[feed] if stamp(a["created_at"]) <= hour]
        if not before:
            raise RuntimeError("MISSING_POINT_IN_TIME_FEED:"+feed)
        artifact = before[0]
        if hour-stamp(artifact["created_at"]) > 6*3600:
            raise RuntimeError("STALE_POINT_IN_TIME_FEED:"+feed)
        validate_run(artifact, ".github/workflows/hourly-game-screener.yml")
        snapshot = compressed_json(github(f"actions/artifacts/{artifact['id']}/zip", True), "snapshot.json.gz")
        if (snapshot.get("schema_version") != "quantura-public-screener-v1" or snapshot.get("feed") != feed
                or stamp(artifact["created_at"]) < stamp(snapshot["published_at"])):
            raise ValueError("INVALID_PUBLIC_SNAPSHOT")
        proof[feed] = artifact["id"]
        for row in snapshot.get("data", {}).get("items", []):
            if model_verified(row) and stamp(row["generated_at"]) <= hour:
                rows.append({**row, "artifact_id": artifact["id"], "publication_ts": stamp(artifact["created_at"])})
    # Shards must not duplicate the same outcome with conflicting predictions.
    unique = {}
    for row in rows:
        key = row["provider"], row["contract_id"]
        if key in unique and unique[key]["predictions"] != row["predictions"]:
            raise ValueError("CONFLICTING_FORECAST_SHARDS")
        unique[key] = row
    return list(unique.values()), proof


def candle_price(value):
    raw = value.get("close_dollars")
    if raw is not None:
        return float(amount(raw))
    raw = value.get("close")
    return None if raw is None else float(amount(raw))/(1 if isinstance(raw, str) and "." in raw else 100)


def market(row):
    symbol = quote(row["symbol"], safe="")
    if row["provider"] == "kalshi":
        data = public_json(KALSHI+"/markets/"+symbol, True)
        if data is None:
            data = public_json(KALSHI+"/historical/markets/"+symbol, True)
        if data is None:
            raise RuntimeError("MARKET_METADATA_MISSING")
        return data.get("market", {})
    data = public_json(POLY+"/v1/market/slug/"+symbol)
    return data.get("market", data.get("data", {}).get("market", {}))


def prices(row, start, end):
    symbol = quote(row["symbol"], safe="")
    if end-start > 86400:
        raise ValueError("PRICE_WINDOW_TOO_WIDE")
    if row["provider"] == "kalshi":
        params = urlencode({"market_tickers": row["symbol"], "start_ts": start, "end_ts": end, "period_interval": 1})
        data = public_json(KALSHI+"/markets/candlesticks?"+params)
        candles = next((m.get("candlesticks", []) for m in data.get("markets", []) if m.get("market_ticker", m.get("ticker")) == row["symbol"]), [])
        if not candles:
            history = public_json(KALSHI+"/historical/markets/"+symbol+"/candlesticks?"+urlencode({"start_ts": start, "end_ts": end, "period_interval": 1}), True)
            candles = history.get("candlesticks", []) if history else []
        points = []
        for c in candles:
            bid, ask = candle_price(c.get("yes_bid", {})), candle_price(c.get("yes_ask", {}))
            if row["side"] != "yes":
                bid, ask = (1-ask if ask is not None else None), (1-bid if bid is not None else None)
            if ask is not None:
                points.append({"at": int(c["end_period_ts"]), "ask": round(ask, 6), "bid": None if bid is None else round(bid, 6)})
        return sorted(points, key=lambda x: x["at"])
    params = urlencode({"symbol": row["symbol"], "timestamp.startTimestamp": start, "timestamp.endTimestamp": end, "fidelity": 1})
    data = public_json(POLY+"/v1/price-history?"+params)
    points = []
    for p in data.get("history", []):
        ask, opposite = p.get("longPrice" if row["side"] == "long" else "shortPrice"), p.get("shortPrice" if row["side"] == "long" else "longPrice")
        if ask is not None:
            points.append({"at": int(p["timestamp"]), "ask": float(amount(ask)), "bid": None if opposite is None else float(1-amount(opposite))})
    return sorted(points, key=lambda x: x["at"])


def candidates_at(rows, hour, registry):
    mapping = identities(rows, registry)
    counts, candidates = Counter(), []
    for row in rows:
        preds = row.get("predictions", [])
        prediction = next((p for p in preds if stamp(p["timestamp"]) == hour), None)
        if (not prediction or not stamp(row["game_start"]) < hour <= stamp(row["forecast_end"])
                or float(preds[-1]["quantiles"]["0.01"]) <= .5):
            continue
        native = row["provider"]+":"+str(row["event_id"])
        fixture = mapping.get(native)
        if not fixture:
            counts["unverified_game_identity"] += 1
            continue
        points = prices(row, hour-60, hour+60)
        observed = [p for p in points if hour-60 < p["at"] <= hour]
        p01 = float(prediction["quantiles"]["0.01"])
        if not observed or not 0 < observed[-1]["ask"] < p01:
            counts["no_below_p01_hour_quote"] += 1
            continue
        follow = [p for p in points if hour < p["at"] <= hour+60 and 0 < p["ask"] < min(1, p01)]
        if not follow:
            counts["no_next_minute_ask"] += 1
            continue
        point, metadata = follow[0], market(row)
        close = metadata.get("close_time" if row["provider"] == "kalshi" else "endDate")
        if not close:
            counts["missing_market_close"] += 1
            continue
        coefficient = metadata.get("feeCoefficient") if row["provider"] == "polymarket_us" else ".07"
        if coefficient is None:
            counts["missing_fee_coefficient"] += 1
            continue
        if row["provider"] == "kalshi":
            # Validate series fee type and event overrides instead of silently
            # assigning the general schedule to a different fee market.
            series = public_json(KALSHI+"/series/"+quote(row["symbol"].split("-", 1)[0], safe="")).get("series", {})
            event_data = public_json(KALSHI+"/events/"+quote(str(row["event_id"]), safe="")).get("event", {})
            if (series.get("fee_type") not in ("quadratic", "quadratic_with_maker_fees")
                    or float(series.get("fee_multiplier", 1)) != 1
                    or event_data.get("fee_multiplier") not in (None, 1, "1", "1.0")):
                counts["unsupported_fee_schedule"] += 1
                continue
        candidates.append({"forecast_id": row["id"], "provider": row["provider"], "symbol": row["symbol"],
                           "contract_id": row["contract_id"], "side": row["side"], "event_id": row["event_id"],
                           "event_title": row["event_title"], "fixture_id": fixture, "identity_verified": True,
                           "seven_model_verified": True, "artifact_id": row["artifact_id"],
                           "forecast_sha256": hashlib.sha256(json.dumps(preds, sort_keys=True).encode()).hexdigest(),
                           "price": point["ask"], "p01": p01, "final_p01": preds[-1]["quantiles"]["0.01"],
                           "signal_at": hour, "decision_at": point["at"], "quote_at": point["at"],
                           "published_at": row["publication_ts"], "game_start": stamp(row["game_start"]),
                           "forecast_end": stamp(row["forecast_end"]), "market_close": stamp(close),
                           "fee_coefficient": coefficient, "recorded_at": int(time.time())})
    return candidates, counts


def update_settlements(state, now):
    cache, marks, errors = {}, {}, Counter()
    for portfolio in state["portfolios"].values():
        for trade in portfolio["trades"]:
            if trade.get("payout") is not None:
                continue
            key = trade["forecast_id"]
            if key not in cache:
                try:
                    metadata, payout = market(trade), None
                    if trade["provider"] == "kalshi" and metadata.get("status") in ("settled", "finalized"):
                        raw = metadata.get("settlement_value_dollars")
                        if raw is None:
                            raw = {"yes": 1, "no": 0}.get(metadata.get("result"))
                        if raw is not None:
                            payout = float(amount(raw)) if trade["side"] == "yes" else float(1-amount(raw))
                    elif trade["provider"] == "polymarket_us":
                        result = public_json(POLY+"/v1/markets/"+quote(trade["symbol"], safe="")+"/settlement", True)
                        raw = result.get("settlement") if result else None
                        if raw is not None:
                            payout = float(amount(raw)) if trade["side"] == "long" else float(1-amount(raw))
                    cache[key] = payout
                    if payout is None and now < trade["market_close"]:
                        samples = [p for p in prices(trade, now-120, now) if p.get("bid") is not None and p["at"] <= now]
                        if samples:
                            marks[key] = {"at": samples[-1]["at"], "bid": samples[-1]["bid"]}
                except (RuntimeError, ValueError, KeyError) as error:
                    errors[type(error).__name__] += 1
                    cache[key] = None
            if cache[key] is not None:
                if not 0 <= cache[key] <= 1:
                    raise ValueError("INVALID_PROVIDER_PAYOUT")
                trade.update(payout=str(cache[key]), settlement_at=now,
                             settlement_time_basis="first_confirmed_paper_receipt")
        portfolio["equity"] = sample_equity(portfolio["trades"], marks, now, portfolio["equity"])
    return dict(errors)


def stats(state, directory):
    result = {"mode": "paper_only", "activation_at": iso(state["activation_at"]),
              "next_hour": iso(state["next_hour"]), "coverage": state["coverage"],
              "firestore_writes": 0, "cloud_storage_writes": 0, "live_orders": 0,
              "portfolios": {}}
    for name, portfolio in state["portfolios"].items():
        value = {**performance(portfolio["trades"]), "equity": portfolio["equity"]}
        result["portfolios"][name] = value
        event("game_p01_paper_stats", portfolio=name, **value)
        print(f"{name} | W/L={value['wins']}/{value['losses']} win={value['win_rate_pct']}% "
              f"net=${value['net_pnl_usd']:.4f} realized DD=${value['max_realized_drawdown_usd']:.4f} "
              f"max streak W/L={value['max_win_streak']}/{value['max_loss_streak']} "
              f"recorded funding=${value['historical_minimum_cash_usd']:.4f} open={value['open_positions']}", flush=True)
    (directory/"stats.json").write_text(json.dumps(result, indent=2)+"\n")
    ledger = [{"portfolio": name, **trade} for name, portfolio in state["portfolios"].items() for trade in portfolio["trades"]]
    fields = ["portfolio", "fixture_id", "provider", "symbol", "side", "event_title", "signal_at", "decision_at",
              "price", "quantity", "entry_cost", "entry_fee", "p01", "final_p01", "payout", "settlement_at"]
    with (directory/"paper-ledger.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(ledger)


def run(directory, now=None):
    now = int(time.time()) if now is None else now
    state = restore(now)
    save(state, directory)
    errors = []
    try:
        catalog = {feed: artifacts("quantura-public-"+feed) for feed in FEEDS}
        # Never silently fast-forward a retention gap. An operator must review it.
        if now-state["next_hour"] > 48*3600:
            raise RuntimeError("CHECKPOINT_COVERAGE_GAP_REQUIRES_REVIEW")
        while state["next_hour"]+120 <= now:
            hour = state["next_hour"]
            rows, proof = snapshots_at(hour, catalog)
            candidates, counts = candidates_at(rows, hour, state["identities"])
            for rule in (BASELINE, SHALLOW):
                portfolio = state["portfolios"][rule.name]
                chosen = select(candidates, [t["fixture_id"] for t in portfolio["trades"]], rule)
                portfolio["trades"] += [fill(c, rule) for c in chosen]
            state["coverage"][str(hour)] = {"candidate_outcomes": len(candidates), "skips": dict(counts), "artifact_ids": proof}
            state["next_hour"] += 3600
            save(state, directory)
            event("paper_hour_replayed", hour=iso(hour), candidates=len(candidates), skips=dict(counts))
        settlement_errors = update_settlements(state, int(time.time()))
        if settlement_errors:
            errors.append({"settlement_reads": settlement_errors})
    except (RuntimeError, ValueError, KeyError) as error:
        # Fail the run visibly, preserving the unprocessed hour and every prior
        # entry. A missing feed cannot silently create a competing venue bias.
        errors.append({"error": str(error)[:180]})
        event("paper_retry_required", error=str(error)[:180])
    finally:
        state["last_run_at"] = int(time.time())
        state["last_errors"] = errors
        save(state, directory)
        stats(state, directory)
    if errors:
        raise RuntimeError("PAPER_TRACKER_RETRY_REQUIRED")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.output)
