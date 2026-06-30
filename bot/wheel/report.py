"""Build docs/wheel.json — what the dashboard's Wheel section renders.

Shows pending option orders (resting sell-to-opens), open option positions
(filled short puts/calls), and per-symbol ledger info (premium, basis, state).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest

from .config import WheelConfig
from .engine import Ledger, build_positions_view, pending_option_underlyings
from .strategy import aggregate_premium, parse_occ, reconstruct_state

log = logging.getLogger("wheel")
WHEEL_FILE = Path(__file__).resolve().parents[2] / "docs" / "wheel.json"


def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _option_fills(t: TradingClient) -> list[dict]:
    """Filled option orders -> premium fills for aggregate_premium()."""
    fills = []
    for o in t.get_orders(GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=500)):
        if not o.filled_at or not o.filled_avg_price or not o.filled_qty:
            continue
        try:
            under, _exp, _typ, _strike = parse_occ(o.symbol)
        except ValueError:
            continue  # equity order
        side = getattr(o.side, "value", str(o.side))
        fills.append({
            "underlying": under, "side": side,
            "credit": _f(o.filled_avg_price) * _f(o.filled_qty) * 100,
        })
    return fills


def build_wheel_report(cfg: WheelConfig) -> dict:
    t = TradingClient(cfg.api_key, cfg.api_secret, paper=cfg.paper)
    acct = t.get_account()
    equity = _f(acct.equity)
    led = Ledger.load()
    pnl = aggregate_premium(_option_fills(t))  # per-underlying premium from fills

    option_positions, pv = [], build_positions_view(t)
    for p in t.get_all_positions():
        ac = getattr(p.asset_class, "value", str(p.asset_class))
        if ac != "us_option":
            continue
        try:
            under, exp, typ, strike = parse_occ(p.symbol)
        except ValueError:
            continue
        option_positions.append({
            "underlying": under, "type": typ, "strike": strike,
            "expiration": exp.isoformat(), "qty": _f(p.qty),
            "market_value": _f(p.market_value), "unrealized_pl": _f(p.unrealized_pl),
        })

    pending = []
    for o in t.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN)):
        try:
            under, exp, typ, strike = parse_occ(o.symbol)
        except ValueError:
            continue
        pending.append({
            "underlying": under, "type": typ, "strike": strike,
            "expiration": exp.isoformat(), "limit": _f(o.limit_price),
            "status": getattr(o.status, "value", str(o.status)),
            "side": getattr(o.side, "value", str(o.side)),
        })

    pend_map = pending_option_underlyings(t)
    symbols = []
    for sym in cfg.universe:
        opt_type = pv.options.get(sym, {}).get("type")
        share_qty = pv.shares.get(sym, {}).get("qty", 0.0)
        state = reconstruct_state(opt_type, share_qty)
        if sym in pend_map:
            state_label = "ORDER_PENDING"
        else:
            state_label = state.value
        p = pnl.get(sym, {})
        symbols.append({
            "symbol": sym, "state": state_label,
            "premium_collected": p.get("gross_premium", 0.0),
            "realized_pnl": p.get("realized", 0.0),
        })

    exposure = pv.exposure() + sum(o["strike"] * 100 for o in pending if o["type"] == "put")
    # NOTE: intentionally no high-resolution timestamp here. Running every 5 min,
    # a changing timestamp would make every run commit wheel.json and trigger a
    # Cloudflare Pages rebuild (500/mo free limit). Without it, the file only
    # changes — and only rebuilds — when the actual wheel data changes.
    return {
        "enabled": cfg.enabled,
        "equity": round(equity, 2),
        "exposure_pct": round(exposure / equity * 100, 1) if equity else 0,
        "total_premium_collected": round(sum(s["premium_collected"] for s in symbols), 2),
        "total_realized_pnl": round(sum(s["realized_pnl"] for s in symbols), 2),
        "option_positions": option_positions,
        "pending_orders": pending,
        "symbols": symbols,
    }


def write_wheel_report(cfg: WheelConfig) -> Path:
    WHEEL_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = build_wheel_report(cfg)
    WHEEL_FILE.write_text(json.dumps(data, indent=2))
    log.info("Wrote %s (%d option positions, %d pending orders)",
             WHEEL_FILE, len(data["option_positions"]), len(data["pending_orders"]))
    return WHEEL_FILE
