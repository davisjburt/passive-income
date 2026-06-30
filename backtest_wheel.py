#!/usr/bin/env python3
"""
Backtest the max-aggression wheel strategy on historical data.

Simulates selling cash-secured puts + covered calls on the configured universe
using Black-Scholes pricing with realized-vol-derived implied volatility.

Usage:
    python backtest_wheel.py
    python backtest_wheel.py --start 2022-01-01
"""

from __future__ import annotations

import argparse
from datetime import datetime

import numpy as np
import pandas as pd
from scipy.stats import norm

try:
    import yfinance as yf
except ImportError:
    raise SystemExit("pip install yfinance")

# ── Strategy params (mirrors config.wheel.yaml aggressive settings) ─────────
UNIVERSE    = ["MARA", "RIOT", "AMC", "SOFI", "NIO", "F", "AAL", "CLF", "VALE"]
EQUITY      = 10_000.0   # starting account size
PER_CAP     = 0.25       # max per position (25% = $2,500)
PORT_CAP    = 0.95       # max total deployment
MAX_TICKERS = 8
OTM_PUT     = 0.05       # 5% OTM puts  (midpoint of 2–8%)
OTM_CALL    = 0.035      # 3.5% OTM calls (midpoint of 2–5%)
DTE         = 14         # days to expiry (midpoint of 7–21)
MIN_YIELD   = 0.20       # 20% annualized minimum
RF          = 0.05       # risk-free rate

# IV is estimated from 21-day realized vol scaled up by this factor.
# Options implied vol historically trades ~20-40% above realized — higher for
# these high-IV names so we use 1.4.
IV_SCALE    = 1.4
IV_FLOOR    = 0.30       # never below 30% IV
IV_CAP      = 8.0        # cap at 800% (SNDL/penny names can go nuts)


# ── Black-Scholes ────────────────────────────────────────────────────────────

def _bs(flag: str, S: float, K: float, T: float, r: float, σ: float) -> float:
    if T <= 0 or σ <= 0 or S <= 0 or K <= 0:
        return max(S - K, 0.0) if flag == "c" else max(K - S, 0.0)
    d1 = (np.log(S / K) + (r + 0.5 * σ ** 2) * T) / (σ * np.sqrt(T))
    d2 = d1 - σ * np.sqrt(T)
    if flag == "c":
        return S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
    return K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def _iv(prices: pd.Series) -> pd.Series:
    rv = np.log(prices / prices.shift(1)).rolling(21).std() * np.sqrt(252)
    return (rv * IV_SCALE).clip(IV_FLOOR, IV_CAP).ffill().bfill()


# ── Per-symbol simulation ─────────────────────────────────────────────────────

def simulate(sym: str, prices: pd.Series, allocation: float) -> dict:
    """
    Simulate the wheel on one symbol with the given dollar allocation.
    Cycles through: CASH → PUT_OPEN → (assigned?) LONG_STOCK → CALL_OPEN → repeat.
    """
    iv = _iv(prices)
    dates = prices.index

    state = "CASH"
    put_strike = call_strike = cost_basis = 0.0
    expiry_i = 0

    total_premium = 0.0   # gross premium collected
    stock_pnl = 0.0       # gains / losses from stock positions
    assignments = 0
    calls_away = 0
    cycles = 0
    equity_pts: list[tuple] = []

    i = 25  # skip IV warmup period
    while i < len(dates):
        S = float(prices.iloc[i])
        σ = float(iv.iloc[i])
        if not np.isfinite(S) or S <= 0 or not np.isfinite(σ):
            i += 1
            continue

        # ── Expiry handler ──────────────────────────────────────────────────
        if i >= expiry_i and state in ("PUT_OPEN", "CALL_OPEN"):
            if state == "PUT_OPEN":
                if S < put_strike:                   # assigned
                    cost_basis = put_strike
                    state = "LONG_STOCK"
                    assignments += 1
                else:                                # expired worthless
                    state = "CASH"
            else:                                    # CALL_OPEN
                if S > call_strike:                  # called away
                    stock_pnl += (call_strike - cost_basis) * 100
                    calls_away += 1
                    state = "CASH"
                else:                                # not called, sell another
                    state = "LONG_STOCK"
            i += 1
            continue

        # ── Log equity ─────────────────────────────────────────────────────
        if state in ("LONG_STOCK", "CALL_OPEN"):
            approx_eq = total_premium + stock_pnl + (S - cost_basis) * 100
        else:
            approx_eq = total_premium + stock_pnl
        equity_pts.append((dates[i], allocation + approx_eq))

        # ── Open new leg ────────────────────────────────────────────────────
        if state == "CASH":
            K = round(S * (1 - OTM_PUT), 2)
            if K * 100 > allocation:             # contract too big for allocation
                i += 1
                continue
            T = DTE / 365
            prem = _bs("p", S, K, T, RF, σ) * 100
            if prem / (K * 100) * (365 / DTE) < MIN_YIELD:
                i += 1
                continue
            put_strike = K
            total_premium += prem
            cycles += 1
            state = "PUT_OPEN"
            expiry_i = min(i + DTE, len(dates) - 1)
            i = expiry_i  # jump forward to expiry

        elif state == "LONG_STOCK":
            ref = max(cost_basis, S)
            K = round(ref * (1 + OTM_CALL), 2)
            T = DTE / 365
            prem = _bs("c", S, K, T, RF, σ) * 100
            if prem / (S * 100) * (365 / DTE) < MIN_YIELD:
                i += 1
                continue
            call_strike = K
            total_premium += prem
            cycles += 1
            state = "CALL_OPEN"
            expiry_i = min(i + DTE, len(dates) - 1)
            i = expiry_i

        else:
            i += 1

    # ── Final liquidation ───────────────────────────────────────────────────
    if state in ("LONG_STOCK", "CALL_OPEN"):
        stock_pnl += (float(prices.iloc[-1]) - cost_basis) * 100

    net_pnl = total_premium + stock_pnl
    days = max((dates[-1] - dates[25]).days, 1)
    total_ret = net_pnl / allocation
    ann_ret = (1 + total_ret) ** (365 / days) - 1

    # Equity curve as a proper series
    if equity_pts:
        eq_df = pd.DataFrame(equity_pts, columns=["date", "equity"]).set_index("date")
        eq_df = eq_df[~eq_df.index.duplicated(keep="last")].resample("W").last().ffill()
    else:
        eq_df = pd.DataFrame()

    return dict(
        sym=sym,
        allocation=allocation,
        net_pnl=net_pnl,
        total_ret_pct=total_ret * 100,
        ann_ret_pct=ann_ret * 100,
        premium=total_premium,
        stock_pnl=stock_pnl,
        cycles=cycles,
        assignments=assignments,
        calls_away=calls_away,
        assign_rate=assignments / max(cycles, 1) * 100,
        eq_df=eq_df,
    )


# ── Portfolio-level aggregation ───────────────────────────────────────────────

def run_backtest(start: str, end: str) -> None:
    print(f"\n{'='*62}")
    print(f"  Wheel Backtest  |  {start} → {end}")
    print(f"  Universe: {', '.join(UNIVERSE)}")
    print(f"  DTE={DTE}  OTM put={OTM_PUT*100:.0f}%  OTM call={OTM_CALL*100:.1f}%")
    print(f"  MinYield={MIN_YIELD*100:.0f}%/yr  Account=${EQUITY:,.0f}")
    print(f"{'='*62}\n")

    # ── Fetch prices ────────────────────────────────────────────────────────
    print("Fetching historical prices…")
    raw = yf.download(UNIVERSE, start=start, end=end, auto_adjust=True, progress=False)
    closes = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw
    print(f"Got {len(closes)} trading days of data.\n")

    # ── Simulate each symbol ─────────────────────────────────────────────────
    allocation = EQUITY * PER_CAP   # $2,500 per slot
    results = []
    for sym in UNIVERSE:
        if sym not in closes.columns:
            print(f"  {sym}: no data, skipping")
            continue
        prices = closes[sym].dropna()
        if len(prices) < 60:
            print(f"  {sym}: insufficient history, skipping")
            continue
        r = simulate(sym, prices, allocation)
        results.append(r)

    if not results:
        print("No results.")
        return

    # ── Per-symbol table ─────────────────────────────────────────────────────
    print(f"{'Symbol':<8} {'Alloc':>8} {'Net P/L':>10} {'Return':>8} {'Ann.Ret':>8} "
          f"{'Premium':>10} {'StockPnL':>10} {'Cycles':>7} {'Assign%':>8}")
    print("-" * 82)

    portfolio_pnl = 0.0
    portfolio_premium = 0.0
    for r in results:
        sign = "+" if r["net_pnl"] >= 0 else ""
        print(
            f"{r['sym']:<8} "
            f"${r['allocation']:>7,.0f} "
            f"{sign}${r['net_pnl']:>8,.0f} "
            f"{sign}{r['total_ret_pct']:>6.1f}% "
            f"{'+' if r['ann_ret_pct'] >= 0 else ''}{r['ann_ret_pct']:>6.1f}% "
            f"  ${r['premium']:>8,.0f} "
            f"  ${r['stock_pnl']:>+8,.0f} "
            f"  {r['cycles']:>5} "
            f"  {r['assign_rate']:>5.1f}%"
        )
        portfolio_pnl += r["net_pnl"]
        portfolio_premium += r["premium"]

    print("-" * 82)

    # ── Portfolio summary ────────────────────────────────────────────────────
    # In practice, $10k can fund ~4 simultaneous $2,500 positions (95% deployed).
    # Scale portfolio P/L to reflect that constraint (divide by n_syms, multiply by 4).
    n = len(results)
    slots = min(n, int(EQUITY * PORT_CAP / allocation))
    scale = slots / n   # fraction of time portfolio is actually running each symbol

    scaled_pnl = portfolio_pnl * scale
    scaled_premium = portfolio_premium * scale
    days = max((closes.index[-1] - closes.index[25]).days, 1)
    port_ret = scaled_pnl / EQUITY
    port_ann = (1 + port_ret) ** (365 / days) - 1

    print(f"\n{'─'*62}")
    print(f"  Portfolio summary  (${EQUITY:,.0f} account, {slots} simultaneous slots)")
    print(f"{'─'*62}")
    print(f"  Gross premium collected : ${scaled_premium:>10,.0f}")
    print(f"  Net P/L                 : ${scaled_pnl:>+10,.0f}")
    print(f"  Total return            : {port_ret*100:>+8.1f}%")
    print(f"  Annualized return       : {port_ann*100:>+8.1f}%")
    print(f"  Period                  :  {days} days ({days/365:.1f} yrs)")
    print(f"  Total option cycles     :  {int(sum(r['cycles'] for r in results) * scale)}")
    print(f"  Total assignments       :  {int(sum(r['assignments'] for r in results) * scale)}")
    print(f"  Avg assignment rate     :  {sum(r['assign_rate'] for r in results)/n:.1f}%")

    # ── Buy-and-hold comparison ──────────────────────────────────────────────
    print(f"\n{'─'*62}")
    print(f"  Buy-and-hold comparison (equal-weight portfolio)")
    print(f"{'─'*62}")
    print(f"  {'Symbol':<8} {'Start':>8} {'End':>8} {'Return':>10} {'Ann.Ret':>10}")
    bh_rets = []
    for sym in UNIVERSE:
        if sym not in closes.columns:
            continue
        p = closes[sym].dropna()
        if len(p) < 2:
            continue
        ret = (p.iloc[-1] / p.iloc[0] - 1) * 100
        d = (p.index[-1] - p.index[0]).days
        ann = ((1 + ret/100) ** (365/max(d,1)) - 1) * 100
        print(f"  {sym:<8} ${p.iloc[0]:>7.2f}  ${p.iloc[-1]:>6.2f}  "
              f"{ret:>+8.1f}%  {ann:>+8.1f}%/yr")
        bh_rets.append(ann)
    if bh_rets:
        print(f"  {'Equal-wt avg':<20}  {' '*17} {sum(bh_rets)/len(bh_rets):>+8.1f}%/yr")

    print(f"\n{'─'*62}")
    print("  NOTE: Backtest uses Black-Scholes with 21-day realized vol ×1.4")
    print("  as an IV proxy. Real fills may differ. Past performance ≠ future.")
    print(f"{'─'*62}\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--end",   default=datetime.today().strftime("%Y-%m-%d"))
    args = ap.parse_args()
    run_backtest(args.start, args.end)
