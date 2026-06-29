"""Pure wheel logic: state machine, contract selection, sizing caps, yield.

No I/O here — every function takes plain values so it is fully unit-testable.
The Alpaca-touching layer lives in engine.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from enum import Enum


class WheelState(str, Enum):
    CASH = "CASH"            # no shares, no open option
    PUT_OPEN = "PUT_OPEN"    # short a cash-secured put
    LONG_STOCK = "LONG_STOCK"  # hold >=100 shares, no open call
    CALL_OPEN = "CALL_OPEN"  # hold shares + short a covered call


# ---------- state ----------

def reconstruct_state(open_option_type: str | None, share_qty: float) -> WheelState:
    """Source of truth is the broker. Map current holdings -> wheel state."""
    if open_option_type == "put":
        return WheelState.PUT_OPEN
    if open_option_type == "call":
        return WheelState.CALL_OPEN
    if share_qty and share_qty >= 100:
        return WheelState.LONG_STOCK
    return WheelState.CASH


_OCC = re.compile(r"^([A-Z]+)(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")


@dataclass
class Contract:
    symbol: str          # OCC option symbol
    underlying: str
    type: str            # 'put' | 'call'
    strike: float
    expiration: date
    bid: float           # credit per share (x100 for one contract)

    def dte(self, today: date) -> int:
        return (self.expiration - today).days


def parse_occ(occ: str) -> tuple[str, date, str, float]:
    """'AAPL250117P00150000' -> ('AAPL', date(2025,1,17), 'put', 150.0)."""
    m = _OCC.match(occ)
    if not m:
        raise ValueError(f"not an OCC option symbol: {occ}")
    root, yy, mm, dd, cp, strike = m.groups()
    exp = date(2000 + int(yy), int(mm), int(dd))
    typ = "call" if cp == "C" else "put"
    return root, exp, typ, int(strike) / 1000.0


# ---------- yield ----------

def annualized_yield(premium_per_contract: float, capital_at_risk: float, dte: int) -> float:
    """premium / capital, annualized. e.g. $200 on $15,000 reserved, 35 DTE -> ~14%."""
    if capital_at_risk <= 0 or dte <= 0:
        return 0.0
    return (premium_per_contract / capital_at_risk) * (365.0 / dte)


# ---------- strike bands ----------

def put_strike_band(spot: float, otm_pct: tuple[float, float]) -> tuple[float, float]:
    lo, hi = otm_pct                       # e.g. (0.10, 0.15)
    return spot * (1 - hi), spot * (1 - lo)  # strikes 10-15% BELOW spot


def call_strike_band(reference: float, otm_above: tuple[float, float]) -> tuple[float, float]:
    lo, hi = otm_above                     # e.g. (0.05, 0.10)
    return reference * (1 + lo), reference * (1 + hi)  # strikes 5-10% ABOVE reference


# ---------- caps ----------

def per_stock_ok(notional: float, equity: float, cap_pct: float) -> bool:
    return notional <= equity * cap_pct + 1e-9


def portfolio_ok(current_exposure: float, added: float, equity: float, cap_pct: float) -> bool:
    return current_exposure + added <= equity * cap_pct + 1e-9


# ---------- selection ----------

@dataclass
class LegRules:
    band: tuple[float, float]   # put: % below spot; call: % above reference
    dte: tuple[int, int]
    min_annual_yield: float


def select_put(candidates, spot, rules: LegRules, today, equity,
               per_stock_cap_pct, exposure, portfolio_cap_pct):
    """Best (highest-yield) CSP contract meeting all filters + caps, or None."""
    lo, hi = put_strike_band(spot, rules.band)
    best = None
    for c in candidates:
        if c.type != "put" or not (lo <= c.strike <= hi):
            continue
        dte = c.dte(today)
        if not (rules.dte[0] <= dte <= rules.dte[1]) or c.bid <= 0:
            continue
        reserve = c.strike * 100
        y = annualized_yield(c.bid * 100, reserve, dte)
        if y < rules.min_annual_yield:
            continue
        if not per_stock_ok(reserve, equity, per_stock_cap_pct):
            continue
        if not portfolio_ok(exposure, reserve, equity, portfolio_cap_pct):
            continue
        if best is None or y > best[1]:
            best = (c, y)
    return best


def aggregate_premium(fills: list[dict]) -> dict[str, dict]:
    """Sum option premium per underlying from FILLED option orders.

    fills: {underlying, side('sell'|'buy'), credit} where credit = price*qty*100.
    Selling collects premium (credit); buying-to-close pays it back (debit).
    Returns per-underlying {gross_premium, debits, realized}. `realized` is
    gross - debits; it's approximate while a short is still open (counts the
    credit before expiry), but exact once positions are closed/expired.
    """
    out: dict[str, dict] = {}
    for f in fills:
        d = out.setdefault(f["underlying"], {"gross_premium": 0.0, "debits": 0.0})
        if f["side"] == "sell":
            d["gross_premium"] += f["credit"]
        else:
            d["debits"] += f["credit"]
    for d in out.values():
        d["realized"] = round(d["gross_premium"] - d["debits"], 2)
        d["gross_premium"] = round(d["gross_premium"], 2)
        d["debits"] = round(d["debits"], 2)
    return out


def select_call(candidates, basis, current_price, rules: LegRules, today):
    """Best covered-call contract 5-10% above max(basis, current price), or None.

    Anchoring on max(basis, price) avoids selling a deep-ITM call that locks in a
    loss or caps away a big run-up.
    """
    ref = max(basis, current_price)
    lo, hi = call_strike_band(ref, rules.band)
    notional = current_price * 100
    best = None
    for c in candidates:
        if c.type != "call" or not (lo <= c.strike <= hi):
            continue
        dte = c.dte(today)
        if not (rules.dte[0] <= dte <= rules.dte[1]) or c.bid <= 0:
            continue
        y = annualized_yield(c.bid * 100, notional, dte)
        if y < rules.min_annual_yield:
            continue
        if best is None or y > best[1]:
            best = (c, y)
    return best
