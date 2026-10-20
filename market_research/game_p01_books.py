"""Public, current order books and validated market constraints; no credentials."""
from __future__ import annotations

import time
from urllib.parse import quote

from . import game_p01_paper as feed
from .game_p01_portfolio import amount


def levels(rows, complement=False):
    result = []
    for price, quantity in rows:
        p, q = amount(price), amount(quantity)
        if complement:
            p = 1-p
        if not 0 <= p <= 1 or q <= 0:
            raise ValueError("INVALID_DISPLAYED_DEPTH")
        result.append([str(p), str(q)])
    if len({p for p, _ in result}) != len(result):
        raise ValueError("DUPLICATE_BOOK_LEVEL")
    return result


def book(row):
    requested = time.time()
    symbol = quote(row["symbol"], safe="")
    source = None
    if row["provider"] == "kalshi":
        if row["side"] not in ("yes", "no"):
            raise ValueError("INVALID_KALSHI_SIDE")
        data = feed.public_json(feed.KALSHI+"/markets/"+symbol+"/orderbook")
        fixed = data["orderbook_fp"]
        yes, no = fixed.get("yes_dollars", []), fixed.get("no_dollars", [])
        bids = levels(yes if row["side"] == "yes" else no)
        asks = levels(no if row["side"] == "yes" else yes, True)
        metadata = feed.market(row)
        opened = (metadata.get("ticker") == row["symbol"] and metadata.get("status") == "active"
                  and feed.stamp(metadata["close_time"]) > time.time())
    elif row["provider"] == "polymarket_us":
        if row["side"] not in ("long", "short"):
            raise ValueError("INVALID_POLYMARKET_SIDE")
        data = feed.public_json(feed.POLY+"/v1/markets/"+symbol+"/book")["marketData"]
        if data["marketSlug"] != row["symbol"]:
            raise ValueError("BOOK_MARKET_MISMATCH")
        def unpack(entries):
            result = []
            for item in entries:
                if item["px"]["currency"] != "USD":
                    raise ValueError("NON_USD_BOOK")
                result.append((item["px"]["value"], item["qty"]))
            return result
        yes_bids, yes_asks = unpack(data.get("bids", [])), unpack(data.get("offers", []))
        bids = levels(yes_bids) if row["side"] == "long" else levels(yes_asks, True)
        asks = levels(yes_asks) if row["side"] == "long" else levels(yes_bids, True)
        opened = data.get("state") == "MARKET_STATE_OPEN"
        source = feed.stamp(data["transactTime"]) if data.get("transactTime") else None
    else:
        raise ValueError("UNSUPPORTED_TRADING_VENUE")
    if bids and asks and max(amount(p) for p, _ in bids) > min(amount(p) for p, _ in asks):
        raise ValueError("CROSSED_ORDER_BOOK")
    return {"bids": bids, "asks": asks, "open": opened, "requested_at": requested,
            "received_at": time.time(), "source_at": source}


def constraints(row):
    metadata = feed.market(row)
    if row["provider"] == "kalshi":
        if (metadata.get("ticker") != row["symbol"] or metadata.get("status") != "active"
                or metadata.get("market_type") != "binary"
                or type(metadata.get("exchange_index")) is not int or metadata["exchange_index"] < 0):
            raise ValueError("MARKET_NOT_ACTIVE_BINARY")
        event = feed.public_json(feed.KALSHI+"/events/"+quote(row["event_id"], safe=""))["event"]
        series = feed.public_json(feed.KALSHI+"/series/"+quote(row["symbol"].split("-", 1)[0], safe=""))["series"]
        # Fee overrides must not be silently priced using the general schedule.
        if (series.get("fee_type") not in ("quadratic", "quadratic_with_maker_fees")
                or amount(series.get("fee_multiplier", 1)) != 1
                or event.get("fee_type_override") not in (None, "", "quadratic", "quadratic_with_maker_fees")
                or any(event.get(k) is not None and amount(event[k]) != 1
                       for k in ("fee_multiplier", "fee_multiplier_override"))):
            raise ValueError("UNSUPPORTED_FEE_SCHEDULE")
        step = ".01" if metadata.get("fractional_trading_enabled") is True else "1"
        return {"price_ranges": metadata["price_ranges"], "quantity_step": step,
                "minimum_quantity": step, "market_close": int(feed.stamp(metadata["close_time"])),
                "fee_coefficient": ".07", "exchange_index": metadata["exchange_index"]}
    if row["provider"] == "polymarket_us":
        # Unknown states are rejected. The book must independently be OPEN.
        if metadata.get("active") is not True or metadata.get("closed") is not False:
            raise ValueError("MARKET_NOT_ACTIVE")
        tick, minimum = amount(metadata["orderPriceMinTickSize"]), amount(metadata["minimumTradeQty"])
        if not 0 < tick < 1 or minimum not in (amount(".01"), amount(1)):
            raise ValueError("UNSUPPORTED_MARKET_INCREMENTS")
        coefficient = amount(metadata["feeCoefficient"])
        if not 0 <= coefficient <= 1:
            raise ValueError("UNSUPPORTED_FEE_SCHEDULE")
        return {"price_ranges": [{"start": "0", "end": "1", "step": str(tick)}],
                "quantity_step": str(minimum), "minimum_quantity": str(minimum),
                "market_close": int(feed.stamp(metadata["endDate"])), "fee_coefficient": str(coefficient)}
    raise ValueError("UNSUPPORTED_TRADING_VENUE")


def payout(row):
    """Only final official resolutions, never a last-trade or forecast price."""
    if row["provider"] == "kalshi":
        metadata = feed.market(row)
        if metadata.get("status") not in ("settled", "finalized"):
            return None
        raw = metadata.get("settlement_value_dollars")
        if raw is None:
            raw = {"yes": 1, "no": 0}.get(metadata.get("result"))
    else:
        result = feed.public_json(feed.POLY+"/v1/markets/"+quote(row["symbol"], safe="")+"/settlement", True)
        raw = result.get("settlement") if result else None
    if raw is None:
        return None
    value = amount(raw)
    if not 0 <= value <= 1:
        raise ValueError("INVALID_FINAL_PAYOUT")
    return value if row["side"] in ("yes", "long") else 1-value
