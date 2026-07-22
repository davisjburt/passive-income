"""Build docs/momentum_<account>.json — what the dashboard's Momentum section
renders. Recomputed fresh from broker state + live prices every run (mirrors
bot/wheel/report.py's pattern), independent of whether today's cycle actually
rebalanced anything, so the dashboard shows current standings daily even
though the account itself only trades once a month.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.trading.client import TradingClient

from .config import MomentumConfig
from .engine import _current_holding, fetch_trailing_prices, load_state
from .strategy import trailing_return

log = logging.getLogger("momentum")
DOCS_DIR = Path(__file__).resolve().parents[2] / "docs"


def momentum_file_path(account: str = "momentum") -> Path:
    return DOCS_DIR / f"momentum_{account}.json"


def build_momentum_report(cfg: MomentumConfig) -> dict:
    trading = TradingClient(cfg.api_key, cfg.api_secret, paper=cfg.paper)
    data = StockHistoricalDataClient(cfg.api_key, cfg.api_secret)
    state = load_state(cfg.account)

    universe = list(cfg.risky_universe) + [cfg.safe_symbol]
    today = datetime.now(timezone.utc).date()
    prices = fetch_trailing_prices(data, universe, cfg.lookback_months, today)
    trailing_returns = {
        sym: round(trailing_return(price_now, price_then), 4)
        for sym, (price_then, price_now) in prices.items()
    }

    held_symbol = _current_holding(trading, universe)
    position = None
    if held_symbol:
        p = trading.get_open_position(held_symbol)
        position = {
            "symbol": p.symbol,
            "qty": float(p.qty),
            "avg_entry": float(p.avg_entry_price),
        }

    return {
        "enabled": cfg.enabled,
        "risky_universe": cfg.risky_universe,
        "safe_symbol": cfg.safe_symbol,
        "lookback_months": cfg.lookback_months,
        "current_holding": held_symbol or state.get("current_holding"),
        "position": position,
        "last_rebalance_month": state.get("last_rebalance_month"),
        "trailing_returns": trailing_returns,
        "history": state.get("history", []),
    }


def write_momentum_report(cfg: MomentumConfig) -> Path:
    path = momentum_file_path(cfg.account)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = build_momentum_report(cfg)
    path.write_text(json.dumps(data, indent=2))
    log.info("Wrote %s (holding=%s)", path, data["current_holding"])
    return path
