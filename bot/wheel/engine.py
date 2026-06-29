"""Wheel engine: broker I/O, per-symbol ledger, and the once-per-day cycle.

State is reconstructed from broker positions each run (stateless-CI safe). The
ledger (docs/wheel_ledger.json) only tracks things the broker doesn't hand us
cleanly: cumulative premium collected, effective cost basis, and entry price for
the drawdown safeguard.

NOTE: the option-data and order-placement paths require Alpaca options Level 1 and
have not been exercised live yet — run with --dry-run first and confirm the logged
contracts/quotes look right before removing it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionChainRequest, StockLatestQuoteRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import ContractType, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, LimitOrderRequest

from .config import WheelConfig
from .strategy import (
    Contract,
    WheelState,
    annualized_yield,
    call_strike_band,
    parse_occ,
    put_strike_band,
    reconstruct_state,
    select_call,
    select_put,
)

log = logging.getLogger("wheel")
LEDGER_PATH = Path(__file__).resolve().parents[2] / "docs" / "wheel_ledger.json"


# ---------------- ledger ----------------

@dataclass
class SymbolLedger:
    premium_collected: float = 0.0
    realized_pnl: float = 0.0
    cost_basis: float = 0.0   # set on assignment = strike - put premium/share
    entry_price: float = 0.0  # underlying price when the wheel started (for drawdown)


class Ledger:
    def __init__(self, data: dict | None = None):
        self.data: dict[str, SymbolLedger] = {
            k: SymbolLedger(**v) for k, v in (data or {}).items()
        }

    @classmethod
    def load(cls) -> "Ledger":
        if LEDGER_PATH.exists():
            try:
                return cls(json.loads(LEDGER_PATH.read_text()))
            except json.JSONDecodeError:
                pass
        return cls()

    def save(self) -> None:
        LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        LEDGER_PATH.write_text(json.dumps(
            {k: vars(v) for k, v in self.data.items()}, indent=2))

    def get(self, sym: str) -> SymbolLedger:
        return self.data.setdefault(sym, SymbolLedger())

    def drawdown(self, sym: str, current_price: float) -> float:
        e = self.get(sym).entry_price
        return (e - current_price) / e if e > 0 else 0.0


# ---------------- broker views ----------------

@dataclass
class PositionsView:
    shares: dict = field(default_factory=dict)   # sym -> {qty, price, basis}
    options: dict = field(default_factory=dict)  # underlying -> {type, occ, strike, exp}

    def exposure(self) -> float:
        share_notional = sum(p["qty"] * p["price"] for p in self.shares.values())
        put_cash = sum(o["strike"] * 100 for o in self.options.values() if o["type"] == "put")
        return share_notional + put_cash

    def wheel_names(self) -> set[str]:
        return set(self.shares) | set(self.options)


def build_positions_view(trading: TradingClient) -> PositionsView:
    pv = PositionsView()
    for p in trading.get_all_positions():
        ac = getattr(p.asset_class, "value", str(p.asset_class))
        if ac == "us_option":
            try:
                under, exp, typ, strike = parse_occ(p.symbol)
            except ValueError:
                continue
            pv.options[under] = {"type": typ, "occ": p.symbol, "strike": strike, "exp": exp}
        else:  # us_equity
            pv.shares[p.symbol] = {
                "qty": float(p.qty),
                "price": float(p.current_price or 0),
                "basis": float(p.avg_entry_price or 0),
            }
    return pv


def pending_option_underlyings(trading: TradingClient) -> dict:
    """Underlyings that already have an OPEN option order, so we don't duplicate.

    A resting sell-to-open order has no position yet, so reconstruct_state would
    otherwise see CASH/LONG_STOCK and fire again. Critical once we run frequently.
    """
    out: dict[str, dict] = {}
    try:
        orders = trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
    except Exception as exc:  # noqa: BLE001
        log.warning("open-orders fetch failed: %s", exc)
        return out
    for o in orders:
        try:
            under, _exp, typ, strike = parse_occ(o.symbol)
        except ValueError:
            continue  # equity order, not an option
        out[under] = {"type": typ, "strike": strike}
    return out


def get_spot(data: StockHistoricalDataClient, symbol: str) -> float | None:
    try:
        q = data.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=symbol))
        qq = q[symbol]
        bid, ask = float(qq.bid_price or 0), float(qq.ask_price or 0)
        if bid and ask:
            return (bid + ask) / 2
        return bid or ask or None
    except Exception as exc:  # noqa: BLE001
        log.warning("spot quote failed for %s: %s", symbol, exc)
        return None


def fetch_contracts(opt: OptionHistoricalDataClient, underlying: str, ctype: str,
                    strike_lo: float, strike_hi: float, dte: tuple[int, int],
                    today: date) -> list[Contract]:
    req = OptionChainRequest(
        underlying_symbol=underlying,
        feed="indicative",  # free feed; switch to "opra" if you have that data plan
        type=ContractType.PUT if ctype == "put" else ContractType.CALL,
        strike_price_gte=round(strike_lo, 2),
        strike_price_lte=round(strike_hi, 2),
        expiration_date_gte=(today + timedelta(days=dte[0])).isoformat(),
        expiration_date_lte=(today + timedelta(days=dte[1])).isoformat(),
    )
    out: list[Contract] = []
    try:
        chain = opt.get_option_chain(req)
    except Exception as exc:  # noqa: BLE001
        log.warning("option chain failed for %s %s: %s", underlying, ctype, exc)
        return out
    for occ, snap in (chain or {}).items():
        try:
            under, exp, typ, strike = parse_occ(occ)
        except ValueError:
            continue
        quote = getattr(snap, "latest_quote", None)
        bid = float(getattr(quote, "bid_price", 0) or 0) if quote else 0.0
        out.append(Contract(occ, under, typ, strike, exp, bid))
    return out


# ---------------- cycle ----------------

def run_wheel_cycle(cfg: WheelConfig, dry_run: bool = True) -> dict:
    trading = TradingClient(cfg.api_key, cfg.api_secret, paper=cfg.paper)
    data = StockHistoricalDataClient(cfg.api_key, cfg.api_secret)
    opt = OptionHistoricalDataClient(cfg.api_key, cfg.api_secret)
    ledger = Ledger.load()

    summary = {"actions": [], "skipped": [], "metrics": {}}

    if not cfg.enabled and not dry_run:
        log.warning("wheel disabled (config.wheel.yaml enabled:false). Nothing to do.")
        return summary

    clock = trading.get_clock()
    if not clock.is_open and not dry_run:
        log.info("Market closed. Skipping.")
        return summary

    acct = trading.get_account()
    equity = float(acct.equity)
    pv = build_positions_view(trading)
    pending = pending_option_underlyings(trading)
    # Count pending sell-to-open puts toward exposure + active names so caps and
    # the max-tickers limit hold even before orders fill.
    exposure = pv.exposure() + sum(p["strike"] * 100 for p in pending.values() if p["type"] == "put")
    active = len(pv.wheel_names() | set(pending))
    today = datetime.now(timezone.utc).date()

    log.info("equity=$%.0f exposure=$%.0f (%.0f%%) names=%d",
             equity, exposure, exposure / equity * 100 if equity else 0, active)

    for sym in cfg.universe:
        if sym in pending:
            summary["skipped"].append(f"{sym}: order already pending")
            continue
        opt_type = pv.options.get(sym, {}).get("type")
        share_qty = pv.shares.get(sym, {}).get("qty", 0.0)
        state = reconstruct_state(opt_type, share_qty)

        if state == WheelState.CASH:
            if active >= cfg.max_wheel_tickers:
                summary["skipped"].append(f"{sym}: at max wheel names"); continue
            spot = get_spot(data, sym)
            if not spot:
                summary["skipped"].append(f"{sym}: no quote"); continue
            if ledger.drawdown(sym, spot) >= cfg.safeguards.halt_new_puts_drawdown_pct:
                summary["skipped"].append(f"{sym}: drawdown halt"); continue
            lo, hi = put_strike_band(spot, cfg.put.band)
            cands = fetch_contracts(opt, sym, "put", lo, hi, cfg.put.dte, today)
            pick = select_put(cands, spot, cfg.put, today, equity,
                              cfg.per_stock_cap_pct, exposure, cfg.portfolio_wheel_cap_pct)
            if not pick:
                summary["skipped"].append(f"{sym}: no put meets filters"); continue
            c, y = pick
            _sell_to_open(trading, c, y, cfg, dry_run, summary)
            if not dry_run and not ledger.get(sym).entry_price:
                # Reference price for the drawdown circuit-breaker on future puts.
                ledger.get(sym).entry_price = spot
            exposure += c.strike * 100
            active += 1

        elif state == WheelState.LONG_STOCK:
            shr = pv.shares[sym]
            basis = ledger.get(sym).cost_basis or shr["basis"]
            cands = fetch_contracts(
                opt, sym, "call",
                *call_strike_band(max(basis, shr["price"]), cfg.call.band),
                cfg.call.dte, today)
            pick = select_call(cands, basis, shr["price"], cfg.call, today)
            if not pick:
                summary["skipped"].append(f"{sym}: no call meets filters"); continue
            c, y = pick
            _sell_to_open(trading, c, y, cfg, dry_run, summary)

        # PUT_OPEN / CALL_OPEN: monitor only. Assignment/expiry handled by Alpaca;
        # the next run reconstructs the resulting state.

        led = ledger.get(sym)
        summary["metrics"][sym] = {
            "state": state.value,
            "premium_collected": round(led.premium_collected, 2),
            "cost_basis": round(led.cost_basis, 2),
            "realized_pnl": round(led.realized_pnl, 2),
        }

    if not dry_run:
        ledger.save()
    summary["exposure_pct"] = round(exposure / equity * 100, 1) if equity else 0
    return summary


def _sell_to_open(trading, c: Contract, yld: float, cfg: WheelConfig, dry_run, summary):
    limit = round(c.bid * (1 - cfg.safeguards.limit_slippage_pct), 2)
    msg = (f"SELL-TO-OPEN {c.type.upper()} {c.symbol} "
           f"strike ${c.strike:.2f} bid ${c.bid:.2f} -> limit ${limit:.2f} "
           f"(~{yld*100:.1f}% ann.)")
    if dry_run or limit <= 0:
        log.info("[dry-run] %s", msg)
        summary["actions"].append(f"[dry-run] {msg}")
        return
    order = trading.submit_order(LimitOrderRequest(
        symbol=c.symbol, qty=1, side=OrderSide.SELL,
        time_in_force=TimeInForce.DAY, limit_price=limit))
    log.info("%s -> order %s", msg, order.id)
    summary["actions"].append(msg)
