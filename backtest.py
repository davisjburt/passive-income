"""Backtest the configured strategy on historical data vs. SPY buy-and-hold.

Uses the SAME parameters as config.yaml so results reflect the live bot.
Daily bars, signals computed on day t-1 and executed at day t's close (no
lookahead). Commission-free, idle cash earns 0% (matches the paper account).

Usage:
    .venv/bin/python backtest.py            # from 2016-01-01
    .venv/bin/python backtest.py 2018-01-01 # custom start
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from bot.client import make_data_client
from bot.config import load_config
from bot.data import rsi, sma

INITIAL = 10_000.0


def fetch_closes(cfg, start: datetime) -> pd.DataFrame:
    client = make_data_client(cfg)
    kwargs = dict(symbol_or_symbols=list(cfg.universe), timeframe=TimeFrame.Day, start=start)
    try:
        from alpaca.data.enums import Adjustment
        req = StockBarsRequest(adjustment=Adjustment.ALL, **kwargs)
    except Exception:
        req = StockBarsRequest(**kwargs)
    df = client.get_stock_bars(req).df
    closes = {}
    for sym in cfg.universe:
        if sym in df.index.get_level_values(0):
            closes[sym] = df.xs(sym)["close"]
    return pd.DataFrame(closes).sort_index()


def metrics(curve: pd.Series, trades: list[float] | None = None) -> dict:
    curve = curve.dropna()
    start_v, end_v = curve.iloc[0], curve.iloc[-1]
    days = (curve.index[-1] - curve.index[0]).days or 1
    years = days / 365.25
    cagr = (end_v / start_v) ** (1 / years) - 1 if start_v > 0 else 0
    rets = curve.pct_change().dropna()
    vol = rets.std() * np.sqrt(252)
    sharpe = (rets.mean() / rets.std() * np.sqrt(252)) if rets.std() > 0 else 0
    dd = (curve / curve.cummax() - 1).min()
    out = {
        "total_return": end_v / start_v - 1,
        "cagr": cagr,
        "max_drawdown": dd,
        "vol": vol,
        "sharpe": sharpe,
        "end_value": end_v,
    }
    if trades is not None:
        wins = [t for t in trades if t > 0]
        out["num_trades"] = len(trades)
        out["win_rate"] = len(wins) / len(trades) if trades else 0
    return out


# Core holding that absorbs all cash not deployed to mean-reversion trades,
# so the account stays ~100% invested. Set to None to disable (pure cash drag).
CORE = "SPY"


def run_backtest(cfg, closes: pd.DataFrame) -> tuple[pd.Series, list[float], list[float]]:
    s, r = cfg.strategy, cfg.risk
    syms = list(closes.columns)
    core = CORE if CORE in closes.columns else None
    sat_syms = [c for c in syms if c != core]  # satellites exclude the core ETF

    rsis = pd.DataFrame({c: rsi(closes[c], s.rsi_period) for c in syms})
    sma_t = pd.DataFrame({c: sma(closes[c], s.trend_sma) for c in syms})
    sma_x = pd.DataFrame({c: sma(closes[c], s.exit_sma) for c in syms})

    dates = closes.index
    start_i = s.trend_sma + 1
    cash = INITIAL
    core_qty = 0.0
    positions: dict[str, dict] = {}  # satellite: sym -> {qty, entry, entry_i}
    equity_curve = {}
    closed_pnl: list[float] = []
    invested_frac: list[float] = []

    for i in range(start_i, len(dates)):
        d = dates[i]
        px = closes.iloc[i]
        prev = i - 1  # decisions use data through prior day (no lookahead)
        core_price = px[core] if core else None

        # --- satellite exits (proceeds to cash) ---
        for sym in list(positions.keys()):
            price = px[sym]
            if pd.isna(price):
                continue
            pos = positions[sym]
            r_prev = rsis[sym].iloc[prev]
            exit_ma = sma_x[sym].iloc[prev]
            held = i - pos["entry_i"]
            hit_stop = price <= pos["entry"] * (1 - r.stop_loss_pct)
            recovered = (not pd.isna(r_prev) and r_prev >= s.rsi_exit) or (
                not pd.isna(exit_ma) and closes[sym].iloc[prev] > exit_ma
            )
            if hit_stop or recovered or held >= s.max_hold_days:
                cash += pos["qty"] * price
                closed_pnl.append((price - pos["entry"]) * pos["qty"])
                del positions[sym]

        sat_val = sum(p["qty"] * px[s2] for s2, p in positions.items() if not pd.isna(px[s2]))
        core_val = core_qty * core_price if core else 0.0
        equity = cash + sat_val + core_val

        # --- satellite entries (funded by selling core if needed) ---
        slots = r.max_positions - len(positions)
        if slots > 0:
            cands = []
            for sym in sat_syms:
                if sym in positions:
                    continue
                price = px[sym]
                rp = rsis[sym].iloc[prev]
                tp = sma_t[sym].iloc[prev]
                if pd.isna(price) or pd.isna(rp) or pd.isna(tp):
                    continue
                if closes[sym].iloc[prev] > tp and rp <= s.rsi_entry:
                    cands.append((rp, sym, price))
            cands.sort()
            target = equity * r.max_position_pct
            for rp, sym, price in cands:
                if slots <= 0:
                    break
                room = equity - sat_val  # never let satellites exceed equity
                notional = min(target, room)
                if notional < 1:
                    break
                if core and cash < notional:  # sell core to fund
                    need = notional - cash
                    core_qty -= need / core_price
                    cash += need
                qty = notional / price
                positions[sym] = {"qty": qty, "entry": price, "entry_i": i}
                cash -= notional
                sat_val += notional
                slots -= 1

        # --- sweep all leftover cash into the core holding (stay ~100% invested) ---
        if core and cash != 0:
            core_qty += cash / core_price
            cash = 0.0

        sat_val = sum(p["qty"] * px[s2] for s2, p in positions.items() if not pd.isna(px[s2]))
        core_val = core_qty * core_price if core else 0.0
        equity = cash + sat_val + core_val
        equity_curve[d] = equity
        invested_frac.append((sat_val + core_val) / equity if equity else 0)

    return pd.Series(equity_curve), closed_pnl, invested_frac


def main() -> int:
    start_str = sys.argv[1] if len(sys.argv) > 1 else "2016-01-01"
    start = datetime.fromisoformat(start_str).replace(tzinfo=timezone.utc)
    cfg = load_config()

    print(f"Fetching {len(cfg.universe)} symbols since {start_str} ...")
    closes = fetch_closes(cfg, start)
    if closes.empty or "SPY" not in closes:
        print("No data returned (free tier may not reach that far back). Try a later start date.")
        return 1
    closes = closes.dropna(how="all")
    print(f"Got {len(closes)} trading days, {closes.index[0].date()} -> {closes.index[-1].date()}\n")

    curve, pnl, inv = run_backtest(cfg, closes)
    strat = metrics(curve, pnl)

    # SPY buy-and-hold over the SAME window the strategy was active.
    spy = closes["SPY"].reindex(curve.index).dropna()
    shares = INITIAL / spy.iloc[0]
    bench = metrics(spy * shares)

    def pct(x):
        return f"{x*100:+.1f}%"

    print("=" * 60)
    print(f"{'METRIC':<22}{'STRATEGY':>18}{'SPY BUY&HOLD':>20}")
    print("-" * 60)
    print(f"{'Total return':<22}{pct(strat['total_return']):>18}{pct(bench['total_return']):>20}")
    print(f"{'CAGR (annualized)':<22}{pct(strat['cagr']):>18}{pct(bench['cagr']):>20}")
    print(f"{'Max drawdown':<22}{pct(strat['max_drawdown']):>18}{pct(bench['max_drawdown']):>20}")
    print(f"{'Volatility (ann.)':<22}{pct(strat['vol']):>18}{pct(bench['vol']):>20}")
    print(f"{'Sharpe (rf=0)':<22}{strat['sharpe']:>18.2f}{bench['sharpe']:>20.2f}")
    print(f"{'Ending value ($10k)':<22}{'$'+format(strat['end_value'],',.0f'):>18}{'$'+format(bench['end_value'],',.0f'):>20}")
    print("-" * 60)
    print(f"{'Trades':<22}{strat['num_trades']:>18}{'—':>20}")
    print(f"{'Win rate':<22}{pct(strat['win_rate']):>18}{'—':>20}")
    print(f"{'Avg % invested':<22}{pct(float(np.mean(inv))):>18}{'100.0%':>20}")
    print("=" * 60)
    print("\nNotes: commission-free; idle cash earns 0% (matches paper account);")
    print("dividends included if Alpaca returned adjusted data. Past performance")
    print("does not predict future results.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
