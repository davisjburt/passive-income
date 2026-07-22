"""Momentum engine: broker I/O and the once-a-month rebalance cycle.

Unlike the wheel accounts (every 5 minutes), this only needs to act once a
month, so it's driven by a daily-cadence workflow that no-ops every day except
the first one it runs on in a new calendar month -- see momentum_run.py and
.github/workflows/momentum.yml. State is a single small file
(docs/momentum_state.json) tracking the last month rebalanced and the symbol
currently held, since a 100%-in-one-asset rotation is otherwise easy to
reconstruct from broker positions but the "have I already rebalanced this
month" flag needs to persist across runs.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from .config import MomentumConfig
from .strategy import choose_holding, trailing_return

log = logging.getLogger("momentum")
DOCS_DIR = Path(__file__).resolve().parents[2] / "docs"


def state_path(account: str = "momentum") -> Path:
    return DOCS_DIR / f"momentum_state_{account}.json"


def load_state(account: str = "momentum") -> dict:
    path = state_path(account)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def save_state(state: dict, account: str = "momentum") -> None:
    path = state_path(account)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


def _with_retries(fn, *args, retries: int = 3, what: str = "broker call", **kwargs):
    for attempt in range(1, retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            if attempt == retries:
                raise
            log.warning("%s failed (attempt %d/%d): %s -- retrying", what, attempt, retries, exc)
            time.sleep(2 ** attempt)


def _alert(text: str) -> bool:
    try:
        from bot.notify import send_telegram
        ok = send_telegram(f"📈 *Momentum* — {text}")
        if not ok:
            log.error("Telegram alert dropped after retries: %.80s...", text)
        return ok
    except Exception as exc:  # noqa: BLE001
        log.warning("alert send failed: %s", exc)
        return False


def fetch_trailing_prices(data: StockHistoricalDataClient, symbols: list[str],
                          lookback_months: int, today: date) -> dict[str, tuple[float, float]]:
    """Returns {symbol: (price_then, price_now)} using monthly bars covering
    just over `lookback_months` back, so trailing_return() has both endpoints.
    """
    start = today - timedelta(days=31 * (lookback_months + 1))
    req = StockBarsRequest(symbol_or_symbols=symbols, timeframe=TimeFrame.Month,
                            start=start, end=today, adjustment="all")
    bars = _with_retries(data.get_stock_bars, req, what="get_stock_bars")
    out: dict[str, tuple[float, float]] = {}
    for sym in symbols:
        closes = [b.close for b in bars.data.get(sym, [])]
        if len(closes) >= 2:
            out[sym] = (closes[0], closes[-1])  # (price_then, price_now)
    return out


def run_momentum_cycle(cfg: MomentumConfig, dry_run: bool = True) -> dict:
    trading = TradingClient(cfg.api_key, cfg.api_secret, paper=cfg.paper)
    data = StockHistoricalDataClient(cfg.api_key, cfg.api_secret)
    state = load_state(cfg.account)

    summary = {"rebalanced": False, "holding": state.get("current_holding"), "actions": []}

    if not cfg.enabled and not dry_run:
        log.warning("momentum disabled (config.momentum.yaml enabled:false). Nothing to do.")
        return summary

    clock = _with_retries(trading.get_clock, what="get_clock")
    if not clock.is_open and not dry_run:
        log.info("Market closed. Skipping.")
        return summary

    today = datetime.now(timezone.utc).date()
    month_str = today.strftime("%Y-%m")

    # Trailing returns are fetched every day regardless of whether this month
    # has already been rebalanced -- cheap (one data-API call), and it keeps
    # docs/momentum_<account>.json (the dashboard's source) showing today's
    # standings rather than going stale until next month's actual trade.
    universe = list(cfg.risky_universe) + [cfg.safe_symbol]
    prices = fetch_trailing_prices(data, universe, cfg.lookback_months, today)
    missing = [s for s in universe if s not in prices]
    if missing:
        log.warning("Missing price history for %s -- skipping this cycle.", missing)
        summary["skipped"] = f"missing price history: {missing}"
        return summary

    risky_returns = {}
    for s in cfg.risky_universe:
        price_then, price_now = prices[s]
        risky_returns[s] = trailing_return(price_now, price_then)
    safe_then, safe_now = prices[cfg.safe_symbol]
    safe_return = trailing_return(safe_now, safe_then)
    target = choose_holding(risky_returns, cfg.safe_symbol, safe_return)
    summary["trailing_returns"] = {**risky_returns, cfg.safe_symbol: safe_return}
    summary["target"] = target

    log.info("Trailing %dmo returns: %s | safe(%s)=%.1f%% -> target=%s",
             cfg.lookback_months,
             {s: f"{r*100:.1f}%" for s, r in risky_returns.items()},
             cfg.safe_symbol, safe_return * 100, target)

    if state.get("last_rebalance_month") == month_str:
        log.info("Already rebalanced for %s. Nothing further to do today.", month_str)
        return summary

    current = _current_holding(trading, universe)
    summary["holding"] = current

    def _record_month(holding: str) -> None:
        history = state.get("history", [])
        history.append({"month": month_str, "holding": holding})
        state["history"] = history[-24:]  # cap growth; 2 years of monthly entries is plenty
        state["last_rebalance_month"] = month_str
        state["current_holding"] = holding
        save_state(state, cfg.account)

    if current == target:
        log.info("Already holding %s. No rebalance needed.", target)
        if not dry_run:
            _record_month(target)
        return summary

    msg = f"Rotate {current or 'CASH'} -> {target}"
    summary["actions"].append(msg)
    if dry_run:
        log.info("[dry-run] %s", msg)
        return summary

    if current:
        log.info("Closing existing position in %s", current)
        _with_retries(trading.close_position, current, what=f"close_position[{current}]")
        if not _wait_until_flat(trading, current):
            log.error("Position in %s did not close cleanly -- aborting rebalance this cycle.", current)
            _alert(f"⚠️ Failed to fully close {current} before rotating to {target}. "
                   f"Will retry next cycle rather than risk buying into {target} with unclear cash.")
            return summary

    acct = _with_retries(trading.get_account, what="get_account")
    notional = float(acct.cash) * (1 - cfg.cash_buffer_pct)
    log.info("Buying %s notional $%.2f", target, notional)
    _with_retries(trading.submit_order, MarketOrderRequest(
        symbol=target, notional=round(notional, 2),
        side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
    ), what=f"submit_order[{target}]")

    _record_month(target)
    summary["rebalanced"] = True
    summary["holding"] = target
    _alert(f"🔄 *Rebalanced* — {current or 'CASH'} → *{target}*\n"
           f"Trailing {cfg.lookback_months}mo: " +
           ", ".join(f"{s} {r*100:+.1f}%" for s, r in risky_returns.items()) +
           f", {cfg.safe_symbol} {safe_return*100:+.1f}%")
    return summary


def _current_holding(trading: TradingClient, universe: list[str]) -> str | None:
    positions = _with_retries(trading.get_all_positions, what="get_all_positions")
    held = {p.symbol for p in positions if p.symbol in universe and float(p.qty) > 0}
    if not held:
        return None
    if len(held) > 1:
        log.warning("Multiple universe positions open simultaneously (%s) -- "
                    "unexpected for a 100%%-in-one-asset strategy.", held)
    return next(iter(held))


def _wait_until_flat(trading: TradingClient, symbol: str, timeout_s: int = 30, poll_s: int = 3) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            trading.get_open_position(symbol)
        except Exception:  # noqa: BLE001 -- Alpaca raises when there's no position, which is success
            return True
        time.sleep(poll_s)
    return False
