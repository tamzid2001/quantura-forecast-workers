"""Independent paper comparisons classified at the completed signal hour.

The original forecast defines both branches. Opposite entries use their own ask
and settlement side; a complement of the original ask is never an entry price.
"""
from dataclasses import asdict, dataclass
from decimal import ROUND_DOWN
import hashlib
import json

from .game_p01_portfolio import amount, eligible, fee


@dataclass(frozen=True)
class HourlyRules:
    name: str
    label: str
    opposite: bool = False
    band: str = "0.10"
    notional_usd: str = "1.00"
    model_policy: str = "seven_models_auto_rolling_loss_selection_v2"
    execution: str = "next_recorded_minute_ask_quote_proxy"

    def fingerprint(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


WITHIN = HourlyRules("p01_hour_within_10c_v1", "Within 10c — signaled side")
OUTSIDE = HourlyRules("p01_hour_outside_10c_opposite_v1", "Outside 10c — opposite side", opposite=True)
HOURLY_RULES = (WITHIN, OUTSIDE)
# Keep the retrospective campaign's two rules immutable. This new portfolio
# follows only the WITHIN parent's first choice; it never searches for a replacement.
TIGHT = HourlyRules("p01_hour_within_2c_first_choice_v1", "Within 2c — original first choice", band="0.02")
FORWARD_HOURLY_RULES = (*HOURLY_RULES, TIGHT)
OPPOSITE = {"yes": "no", "no": "yes", "long": "short", "short": "long"}
VENUE_SIDES = {"kalshi": {"yes", "no"}, "polymarket_us": {"long", "short"}}


def ensure_portfolios(state, now):
    """Upgrade old checkpoints without reclassifying their historical entries."""
    for rule in FORWARD_HOURLY_RULES:
        state["portfolios"].setdefault(rule.name, {
            "rules_hash": rule.fingerprint(), "activation_at": int(now),
            "first_signal_at": (int(now)//3600+1)*3600,
            "label": rule.label, "trades": [], "equity": {}, "coverage": {}})


def entry_quote(candidate, rule):
    """Validate both timestamps and venue units before making a paper decision."""
    c = candidate
    if not eligible(c):
        raise ValueError("ORIGINAL_SIGNAL_INELIGIBLE")
    if c.get("side") not in VENUE_SIDES.get(c.get("provider"), set()):
        raise ValueError("INVALID_ECONOMIC_SIDE")
    at, hour = amount(c["signal_quote_at"]), amount(c["signal_at"])
    signal_ask, p01 = amount(c["signal_ask"]), amount(c["p01"])
    if not hour-60 < at <= hour or not 0 < signal_ask < p01 <= 1:
        raise ValueError("INVALID_COMPLETED_HOUR_SIGNAL")
    discount = p01-signal_ask
    if (discount > amount(rule.band)) != rule.opposite:
        raise ValueError("OTHER_HOURLY_DISCOUNT_REGION")
    # Prices are from the same next-minute receipt, already validated by the
    # provider adapter. The opposite ask comes from 1 minus ORIGINAL BID.
    price = amount(c["opposite_ask"] if rule.opposite else c["price"])
    if not amount(".01") <= price < 1:
        raise ValueError("MISSING_OR_INVALID_ENTRY_ASK")
    if rule.opposite and price+amount(c["price"]) < 1:
        raise ValueError("CROSSED_COMPLEMENTARY_QUOTES")
    step, minimum = amount(c["quantity_step"]), amount(c["minimum_quantity"])
    if step not in (amount(".01"), amount(1)) or minimum != step:
        raise ValueError("UNSUPPORTED_QUANTITY_RULES")
    quantity = (amount(rule.notional_usd)/price/step).to_integral_value(rounding=ROUND_DOWN)*step
    if quantity < minimum:
        raise ValueError("NOTIONAL_BELOW_MARKET_MINIMUM")
    paid_fee = fee(c["provider"], quantity, price, c["fee_coefficient"])
    return price, quantity, paid_fee, discount


def select_hourly(candidates, used_fixtures, rule, first_signal_at):
    """Use the original signal's known discount to rank; never read outcomes."""
    ranked, skips = [], {}
    for c in candidates:
        if c["signal_at"] < first_signal_at:
            continue
        try:
            _, _, _, discount = entry_quote(c, rule)
        except (ValueError, KeyError, TypeError, ArithmeticError) as error:
            name = str(error) if isinstance(error, ValueError) else "INCOMPLETE_COMPARISON_QUOTE"
            skips[name] = skips.get(name, 0)+1
            continue
        # No opposite-side forecast is invented. The ORIGINAL signal ranks
        # candidate propositions, just as it does in the reference replay.
        edge = amount(c["p01"])-amount(c["price"])-fee(c["provider"], 1, c["price"], c["fee_coefficient"])
        ranked.append((c["decision_at"], -edge, c["provider"], c["contract_id"], c))
    seen, selected = set(used_fixtures), []
    for *_, row in sorted(ranked, key=lambda value: value[:-1]):
        if row["fixture_id"] not in seen:
            selected.append(row)
            seen.add(row["fixture_id"])
    return selected, skips


def fill_hourly(candidate, rule):
    price, quantity, paid_fee, discount = entry_quote(candidate, rule)
    side = OPPOSITE[candidate["side"]] if rule.opposite else candidate["side"]
    entry = {k: v for k, v in candidate.items() if k not in ("payout", "settlement_at", "settlement_time_basis")}
    return {**entry, "original_forecast_id": candidate["forecast_id"],
            "signal_side": candidate["side"], "signal_contract_id": candidate["contract_id"],
            "signal_entry_ask": str(candidate["price"]), "hourly_discount": str(discount),
            "forecast_id": f"{candidate['forecast_id']}:{rule.name}:{side}",
            "contract_id": f"{candidate['symbol']}:{side}", "side": side,
            "price": str(price), "quantity": str(quantity), "entry_cost": str(price*quantity),
            "entry_fee": str(paid_fee), "rules_hash": rule.fingerprint(),
            "accounting": "recorded_ask_quote_proxy_no_depth_or_slippage", "mode": "paper_only"}


def follow_tight(parent_choices, portfolio, hour):
    """Veto the parent's new first choices using the original hourly ask.

    Call only with candidates selected by WITHIN for this hour. A rejected
    fixture is already owned by that parent's lifetime ledger, so no later
    hour, alternate venue or proposition can replace its rejected first choice.
    """
    chosen, skips = select_hourly(parent_choices, [t["fixture_id"] for t in portfolio["trades"]],
                                  TIGHT, portfolio["first_signal_at"])
    portfolio["trades"] += [fill_hourly(c, TIGHT) for c in chosen]
    if hour >= portfolio["first_signal_at"]:
        portfolio["coverage"][str(hour)] = {"parent_first_choices": len(parent_choices),
                                            "selected_entries": len(chosen), "skips": skips}


def comparison_markdown(report):
    def number(value, prefix="", suffix=""):
        return "N/A" if value is None else f"{prefix}{value:,.2f}{suffix}"
    rules = [r for r in FORWARD_HOURLY_RULES if r.name in report["portfolios"]]
    columns = [report["portfolios"][r.name] for r in rules]
    rows = [
        ("Entries / open", lambda s: f"{s['entries']} / {s['open_positions']}"),
        ("Settled W/L", lambda s: f"{s['wins']} / {s['losses']}"),
        ("Net win rate", lambda s: number(s["win_rate_pct"], suffix="%")),
        ("Net P&L after modeled fees", lambda s: number(s["net_pnl_usd"], "$")),
        ("Modeled fees", lambda s: number(s["fees_usd"], "$")),
        ("Return on settled entry spend", lambda s: number(s["return_on_closed_spend_pct"], suffix="%")),
        ("Max realized drawdown", lambda s: number(s["max_realized_drawdown_usd"], "$")),
        ("Max sampled bid drawdown", lambda s: number(s["equity"].get("max_sampled_bid_drawdown_usd"), "$")),
        ("Current W/L streak", lambda s: f"{s['current_win_streak']} / {s['current_loss_streak']}"),
        ("Longest W/L streak", lambda s: f"{s['max_win_streak']} / {s['max_loss_streak']}"),
        ("Average entry", lambda s: number(s["avg_entry_cents"], suffix=" cents")),
        ("Average time to confirmed settlement", lambda s: number(None if s["avg_duration_seconds"] is None else s["avg_duration_seconds"]/3600, suffix=" hours")),
        ("Open entry cost and fees", lambda s: number(s["open_cost_usd"], "$")),
        ("Recorded cash-flow funding", lambda s: number(s["historical_minimum_cash_usd"], "$")),
        ("Open positions missing a fresh bid", lambda s: str(s["equity"].get("unmarked_positions", 0))),
        ("First eligible signal (UTC)", lambda s: s["first_signal_at"]),
    ]
    lines = ["# Hourly sports P01 paper comparison", "", f"As of {report['as_of']}", "",
             "| Metric | "+" | ".join(r.label.replace(" — ", ": ") for r in rules)+" |",
             "| --- | "+" | ".join("---:" for _ in rules)+" |"]
    lines += ["| "+label+" | "+" | ".join(formatter(s) for s in columns)+" |" for label, formatter in rows]
    lines += ["", "Both use published optimized seven-model forecasts, $1 principal plus modeled fees, provider quantity increments and one lifetime position per game in each portfolio.",
              "", "Next-minute ask quote proxies; not exchange fills or the separate ask-minus-1c limit study. Drawdown is sampled/realized, not continuous. Recorded funding is not a recommended deposit. Retrospective results were not imported.",
              "", "Within 2c follows the within-10c portfolio's first selected game trade, accepting only 0 < hourly P01 minus hourly ask <= 0.02. Rejected games have no replacement entry. Its own future activation excludes historical trades.",
              "", f"Source errors: {len(report['last_errors'])}. Next unprocessed hour: {report['next_hour']}.",
              "", "Download comparison-stats.json and comparison-ledger.csv in the quantura-games-p01-paper-stats artifact for venue breakdowns, signal provenance and full coverage.", ""]
    return "\n".join(lines)
