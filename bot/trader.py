"""Orchestrate one trading cycle: read state -> signals -> orders."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest

from . import risk
from .client import make_data_client, make_trading_client
from .config import Config
from .data import get_daily_bars
from .strategy import evaluate

log = logging.getLogger("bot")

# Alpaca's minimum notional for a fractional order.
MIN_NOTIONAL = 1.0


def _last_buy_dates(trading_client, symbols: list[str]) -> dict[str, datetime]:
    """Best-effort: most recent filled BUY timestamp per symbol (for the time-stop)."""
    try:
        orders = trading_client.get_orders(
            GetOrdersRequest(
                status=QueryOrderStatus.CLOSED,
                side=OrderSide.BUY,
                symbols=symbols,
                limit=200,
            )
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not fetch order history for time-stop: %s", exc)
        return {}
    out: dict[str, datetime] = {}
    for o in orders:
        if o.filled_at is None:
            continue
        prev = out.get(o.symbol)
        if prev is None or o.filled_at > prev:
            out[o.symbol] = o.filled_at
    return out


def run_cycle(cfg: Config, dry_run: bool = False) -> dict:
    trading = make_trading_client(cfg)
    data = make_data_client(cfg)

    summary: dict = {"actions": [], "skipped": [], "halted": False, "market_open": None}

    clock = trading.get_clock()
    summary["market_open"] = bool(clock.is_open)
    if not clock.is_open and not dry_run:
        log.info("Market closed (next open %s). Nothing to do.", clock.next_open)
        return summary

    acct = trading.get_account()
    equity = float(acct.equity)
    cash = float(acct.cash)
    last_equity = float(acct.last_equity)
    summary["equity"] = equity
    log.info(
        "Account: equity=$%.2f cash=$%.2f (since prior close: %+.2f%%)",
        equity, cash, (equity - last_equity) / last_equity * 100 if last_equity else 0.0,
    )

    halted = risk.daily_loss_halt(equity, last_equity, cfg.risk.daily_loss_halt_pct)
    summary["halted"] = halted
    if halted:
        log.warning(
            "DAILY LOSS HALT: down >%.1f%% since prior close. Managing exits only, no new buys.",
            cfg.risk.daily_loss_halt_pct * 100,
        )

    positions = {p.symbol: p for p in trading.get_all_positions()}
    open_orders = trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
    pending = {o.symbol for o in open_orders}
    last_buys = _last_buy_dates(trading, cfg.universe) if cfg.strategy.max_hold_days else {}

    bars = get_daily_bars(data, cfg.universe, cfg.execution.lookback_days)

    # ---- 1. Manage existing positions (exits first; always allowed) ----
    for sym, pos in positions.items():
        if sym in pending:
            summary["skipped"].append(f"{sym}: order already pending")
            continue
        df = bars.get(sym)
        if df is None or df.empty:
            summary["skipped"].append(f"{sym}: no data")
            continue

        avg_entry = float(pos.avg_entry_price)
        price = float(df["close"].iloc[-1])
        sig = evaluate(sym, df, cfg.strategy, holding=True)

        reason = None
        if risk.hit_stop_loss(avg_entry, price, cfg.risk.stop_loss_pct):
            reason = f"STOP-LOSS (entry ${avg_entry:.2f} -> ${price:.2f})"
        elif sym in last_buys and (
            datetime.now(timezone.utc) - last_buys[sym]
        ).days >= cfg.strategy.max_hold_days:
            reason = f"time-stop (>{cfg.strategy.max_hold_days}d)"
        elif sig.action == "sell":
            reason = sig.reason

        if reason:
            _close(trading, sym, reason, dry_run, summary)

    # ---- 2. New entries (skipped entirely if halted) ----
    if halted:
        return summary

    held_or_pending = set(positions) | pending
    slots = cfg.risk.max_positions - len(held_or_pending)
    if slots <= 0:
        log.info("No free position slots (%d held/pending).", len(held_or_pending))
        return summary

    candidates = []
    for sym in cfg.universe:
        if sym in held_or_pending:
            continue
        df = bars.get(sym)
        if df is None or df.empty:
            continue
        sig = evaluate(sym, df, cfg.strategy, holding=False)
        if sig.action == "buy":
            candidates.append(sig)

    # Most oversold first.
    candidates.sort(key=lambda s: s.rsi)

    budget = risk.deployable_cash(equity, cash, cfg.risk.cash_buffer_pct)
    target = risk.position_notional(equity, cfg.risk.max_position_pct)
    log.info("Buy candidates: %s | budget=$%.2f slots=%d",
             [s.symbol for s in candidates] or "none", budget, slots)

    for sig in candidates:
        if slots <= 0:
            break
        notional = round(min(target, budget), 2)
        if notional < MIN_NOTIONAL:
            summary["skipped"].append(f"{sig.symbol}: budget exhausted")
            break
        _buy(trading, sig.symbol, notional, sig.reason, cfg, dry_run, summary)
        budget -= notional
        slots -= 1

    return summary


def _buy(trading, symbol, notional, reason, cfg, dry_run, summary):
    msg = f"BUY  {symbol} ~${notional:.2f} ({reason})"
    if dry_run:
        log.info("[dry-run] %s", msg)
        summary["actions"].append(f"[dry-run] {msg}")
        return
    order = trading.submit_order(
        MarketOrderRequest(
            symbol=symbol,
            notional=notional,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
        )
    )
    log.info("%s -> order %s", msg, order.id)
    summary["actions"].append(msg)


def _close(trading, symbol, reason, dry_run, summary):
    msg = f"SELL {symbol} (close, {reason})"
    if dry_run:
        log.info("[dry-run] %s", msg)
        summary["actions"].append(f"[dry-run] {msg}")
        return
    order = trading.close_position(symbol)
    log.info("%s -> order %s", msg, getattr(order, "id", "ok"))
    summary["actions"].append(msg)
