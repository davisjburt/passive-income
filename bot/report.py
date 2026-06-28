"""Build docs/data.json — the snapshot the static dashboard renders."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from alpaca.trading.requests import GetOrdersRequest, GetPortfolioHistoryRequest
from alpaca.trading.enums import QueryOrderStatus

from .client import make_trading_client
from .config import Config

log = logging.getLogger("bot")

DOCS = Path(__file__).resolve().parent.parent / "docs"
DATA_FILE = DOCS / "data.json"


def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _equity_curve(trading) -> list[dict]:
    try:
        ph = trading.get_portfolio_history(
            GetPortfolioHistoryRequest(period="1A", timeframe="1D")
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("portfolio history unavailable: %s", exc)
        return []
    if not ph.timestamp or not ph.equity:
        return []
    points = []
    for ts, eq in zip(ph.timestamp, ph.equity):
        eq = _f(eq)
        # Skip leading zero-equity days (before the account had any value).
        if not points and eq <= 0:
            continue
        points.append({"t": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d"),
                       "equity": round(eq, 2)})
    return points


def _orders(trading, limit: int = 20) -> list[dict]:
    try:
        orders = trading.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.ALL, limit=limit, nested=False)
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("orders unavailable: %s", exc)
        return []
    out = []
    for o in orders:
        out.append({
            "symbol": o.symbol,
            "side": str(o.side.value if hasattr(o.side, "value") else o.side),
            "qty": _f(o.filled_qty or o.qty),
            "notional": _f(o.notional) if o.notional else None,
            "status": str(o.status.value if hasattr(o.status, "value") else o.status),
            "submitted_at": o.submitted_at.isoformat() if o.submitted_at else None,
            "filled_at": o.filled_at.isoformat() if o.filled_at else None,
            "filled_avg_price": _f(o.filled_avg_price) if o.filled_avg_price else None,
        })
    return out


def build_report(cfg: Config) -> dict:
    trading = make_trading_client(cfg)
    acct = trading.get_account()
    clock = trading.get_clock()

    equity = _f(acct.equity)
    last_equity = _f(acct.last_equity)
    day_pl = equity - last_equity
    day_pl_pct = (day_pl / last_equity * 100) if last_equity else 0.0

    curve = _equity_curve(trading)
    inception = curve[0]["equity"] if curve else equity
    total_pl = equity - inception
    total_pl_pct = (total_pl / inception * 100) if inception else 0.0

    positions = []
    for p in trading.get_all_positions():
        positions.append({
            "symbol": p.symbol,
            "qty": _f(p.qty),
            "avg_entry": _f(p.avg_entry_price),
            "current_price": _f(p.current_price),
            "market_value": _f(p.market_value),
            "unrealized_pl": _f(p.unrealized_pl),
            "unrealized_plpc": _f(p.unrealized_plpc) * 100,
        })

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "market_open": bool(clock.is_open),
        "next_open": clock.next_open.isoformat() if clock.next_open else None,
        "next_close": clock.next_close.isoformat() if clock.next_close else None,
        "account": {
            "equity": round(equity, 2),
            "cash": round(_f(acct.cash), 2),
            "buying_power": round(_f(acct.buying_power), 2),
            "portfolio_value": round(_f(acct.portfolio_value), 2),
            "last_equity": round(last_equity, 2),
            "day_pl": round(day_pl, 2),
            "day_pl_pct": round(day_pl_pct, 2),
            "total_pl": round(total_pl, 2),
            "total_pl_pct": round(total_pl_pct, 2),
        },
        "positions": positions,
        "orders": _orders(trading),
        "equity_curve": curve,
        "strategy": {
            "name": "RSI(2) mean reversion + 200d trend filter",
            "universe": cfg.universe,
            "rsi_entry": cfg.strategy.rsi_entry,
            "rsi_exit": cfg.strategy.rsi_exit,
            "trend_sma": cfg.strategy.trend_sma,
        },
        "risk": {
            "max_positions": cfg.risk.max_positions,
            "max_position_pct": cfg.risk.max_position_pct,
            "cash_buffer_pct": cfg.risk.cash_buffer_pct,
            "stop_loss_pct": cfg.risk.stop_loss_pct,
            "daily_loss_halt_pct": cfg.risk.daily_loss_halt_pct,
        },
    }


def write_report(cfg: Config) -> Path:
    DOCS.mkdir(exist_ok=True)
    data = build_report(cfg)
    DATA_FILE.write_text(json.dumps(data, indent=2))
    log.info("Wrote %s (equity=$%.2f, %d positions)",
             DATA_FILE, data["account"]["equity"], len(data["positions"]))
    return DATA_FILE
