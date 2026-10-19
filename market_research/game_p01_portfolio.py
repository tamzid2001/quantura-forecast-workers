"""Deterministic, read-only game portfolio selection and cash-flow accounting.

Fixture identities must be verified before selection. Outcomes, settlements and
future quotes are deliberately excluded from the candidate ranking.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_HALF_EVEN
import hashlib
import json


def amount(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("NONFINITE_MONEY")
    return result


@dataclass(frozen=True)
class Rules:
    name: str = "p01_one_game_v1"
    notional_usd: str = "1.00"
    final_p01_floor: float = .5
    maximum_discount: float | None = None
    minimum_price: float = .01

    def fingerprint(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


BASELINE = Rules()
SHALLOW = Rules(name="p01_shallow_10c_v1", maximum_discount=.10)


def fee(provider, quantity, price, coefficient=None):
    q, p = amount(quantity), amount(price)
    if q <= 0 or not 0 < p < 1:
        raise ValueError("INVALID_FILL")
    theta = amount(coefficient if coefficient is not None else ".0695" if provider == "polymarket_us" else ".07")
    if not 0 <= theta <= 1:
        raise ValueError("INVALID_FEE_COEFFICIENT")
    raw = theta * q * p * (1-p)
    if provider == "kalshi":
        # Conservative general M=1 total-cash cent rounding, not account fees.
        return (q*p + raw).quantize(Decimal(".01"), rounding=ROUND_CEILING)-q*p
    if provider != "polymarket_us":
        raise ValueError("UNKNOWN_VENUE")
    return raw.quantize(Decimal(".01"), rounding=ROUND_HALF_EVEN)


def eligible(candidate, rules=BASELINE):
    try:
        p, p1 = amount(candidate["price"]), amount(candidate["p01"])
        decision = int(candidate["decision_at"])
        return bool(candidate.get("identity_verified") and candidate.get("fixture_id")
                    and candidate.get("seven_model_verified")
                    and float(candidate["final_p01"]) > rules.final_p01_floor
                    and amount(rules.minimum_price) <= p < p1 <= 1
                    and (rules.maximum_discount is None or p1-p <= amount(rules.maximum_discount))
                    and int(candidate["published_at"]) <= int(candidate["signal_at"]) < decision
                    and decision <= int(candidate["signal_at"])+60
                    and int(candidate["game_start"]) < int(candidate["signal_at"]) <= int(candidate["forecast_end"])
                    and int(candidate["quote_at"]) <= decision <= int(candidate["quote_at"])+60
                    and (not candidate.get("market_close") or decision < int(candidate["market_close"])))
    except (KeyError, ValueError, TypeError, ArithmeticError):
        return False


def select(candidates, used_fixtures=(), rules=BASELINE):
    """One lifetime entry per fixture and portfolio, including other propositions.

    Candidates for an hour are evaluated together at the following minute.
    Rank the lower-bound price discount after the estimated one-contract fee;
    a stable venue/contract order resolves ties without reading outcomes.
    """
    ranked = []
    for row in candidates:
        if not eligible(row, rules):
            continue
        edge = amount(row["p01"])-amount(row["price"])-fee(row["provider"], 1, row["price"], row.get("fee_coefficient"))
        ranked.append((int(row["decision_at"]), -edge, row["provider"], row["contract_id"], row))
    seen, selected = set(used_fixtures), []
    for *_, row in sorted(ranked, key=lambda item: item[:-1]):
        if row["fixture_id"] in seen:
            continue
        seen.add(row["fixture_id"])
        selected.append(dict(row))
    return selected


def fill(candidate, rules=BASELINE):
    if not eligible(candidate, rules):
        raise ValueError("INELIGIBLE_PAPER_ENTRY")
    price = amount(candidate["price"])
    quantity = (amount(rules.notional_usd)/price).quantize(Decimal(".01"), rounding=ROUND_DOWN)
    paid_fee = fee(candidate["provider"], quantity, price, candidate.get("fee_coefficient"))
    return {**candidate, "quantity": str(quantity), "entry_cost": str(quantity*price),
            "entry_fee": str(paid_fee), "rules_hash": rules.fingerprint(),
            "accounting": "recorded_ask_quote_proxy_no_depth_or_slippage", "mode": "paper_only"}


def performance(trades):
    """Realized net includes fees; minimum cash funds the recorded schedule only.

    Credit payouts at first confirmed paper settlement, never before. Entry cash
    debits precede simultaneous settlement credits. This is not future capital
    sufficiency or an exchange's funding minimum.
    """
    if len({t["fixture_id"] for t in trades}) != len(trades):
        raise ValueError("DUPLICATE_GAME_EXPOSURE")
    settled = sorted((t for t in trades if t.get("payout") is not None and t.get("settlement_at") is not None),
                     key=lambda t: (t["settlement_at"], t["fixture_id"]))
    realized = peak = drawdown = Decimal(0)
    wins = losses = breakeven = current_win = current_loss = max_win = max_loss = 0
    cash_events, all_paid, gross = [], Decimal(0), Decimal(0)
    closed_ids = {t["fixture_id"] for t in settled}
    for t in trades:
        cost = amount(t["entry_cost"])+amount(t["entry_fee"])
        if int(t["decision_at"]) < 0 or cost <= 0:
            raise ValueError("INVALID_LEDGER_CASHFLOW")
        cash_events.append((int(t["decision_at"]), 0, t["fixture_id"], -cost))
        all_paid += cost
    for t in settled:
        payout, quantity = amount(t["payout"]), amount(t["quantity"])
        if not 0 <= payout <= 1 or int(t["settlement_at"]) < int(t["decision_at"]):
            raise ValueError("INVALID_SETTLEMENT")
        profit = payout*quantity-amount(t["entry_cost"])-amount(t["entry_fee"])
        gross += payout*quantity-amount(t["entry_cost"])
        realized += profit
        peak = max(peak, realized)
        drawdown = max(drawdown, peak-realized)
        if profit > 0:
            wins += 1; current_win += 1; current_loss = 0
        elif profit < 0:
            losses += 1; current_loss += 1; current_win = 0
        else:
            breakeven += 1; current_win = current_loss = 0
        max_win, max_loss = max(max_win, current_win), max(max_loss, current_loss)
        cash_events.append((int(t["settlement_at"]), 1, t["fixture_id"], payout*quantity))
    cash = minimum = Decimal(0)
    for _, _, _, change in sorted(cash_events):
        cash += change; minimum = min(minimum, cash)
    closed_paid = sum((amount(t["entry_cost"])+amount(t["entry_fee"]) for t in settled), Decimal(0))
    return {"entries": len(trades), "closed_trades": len(settled), "open_positions": len(trades)-len(settled),
            "wins": wins, "losses": losses, "breakevens": breakeven,
            "win_rate_pct": float(Decimal(100)*wins/len(settled)) if settled else None,
            "net_pnl_usd": float(realized), "gross_pnl_usd": float(gross),
            "fees_usd": float(sum((amount(t["entry_fee"]) for t in trades), Decimal(0))),
            "return_on_closed_spend_pct": float(100*realized/closed_paid) if closed_paid else None,
            "max_realized_drawdown_usd": float(drawdown),
            "current_win_streak": current_win, "current_loss_streak": current_loss,
            "max_win_streak": max_win, "max_loss_streak": max_loss,
            "avg_entry_cents": float(sum((amount(t["price"]) for t in trades), Decimal(0))/len(trades)*100) if trades else None,
            "avg_duration_seconds": sum(int(t["settlement_at"])-int(t["decision_at"]) for t in settled)/len(settled) if settled else None,
            "historical_minimum_cash_usd": float(-minimum), "all_entries_without_payout_reuse_usd": float(all_paid),
            "open_cost_usd": float(sum((amount(t["entry_cost"])+amount(t["entry_fee"]) for t in trades if t["fixture_id"] not in closed_ids), Decimal(0))),
            "capital_basis": "recorded_entry_and_first_confirmed_settlement_cashflows_not_future_sufficiency"}


def sample_equity(trades, marks, at, tracking):
    """Sample only a fully marked portfolio; retain missing-mark coverage explicitly."""
    stats = performance(trades)
    open_trades = [t for t in trades if t.get("payout") is None]
    missing, unrealized = [], Decimal(0)
    for trade in open_trades:
        mark = marks.get(trade["forecast_id"])
        if not mark or not 0 <= at-int(mark["at"]) <= 120:
            missing.append(trade["fixture_id"])
            continue
        bid = amount(mark["bid"])
        if not 0 <= bid <= 1:
            missing.append(trade["fixture_id"])
            continue
        unrealized += bid*amount(trade["quantity"])-amount(trade["entry_cost"])-amount(trade["entry_fee"])
    tracking = dict(tracking)
    tracking["checks"] = tracking.get("checks", 0)+1
    tracking["unmarked_positions"] = len(missing)
    tracking["last_checked_at"] = at
    if missing:
        return tracking
    equity = amount(stats["net_pnl_usd"])+unrealized
    peak = max(amount(tracking.get("peak_usd", 0)), equity)
    tracking.update(samples=tracking.get("samples", 0)+1, last_complete_at=at,
                    sampled_pnl_usd=float(equity), peak_usd=float(peak),
                    max_sampled_bid_drawdown_usd=float(max(amount(tracking.get("max_sampled_bid_drawdown_usd", 0)), peak-equity)))
    return tracking
