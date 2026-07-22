"""Historical backtest of a dual/absolute-momentum ETF rotation strategy
against buy-and-hold SPY and QQQ, over as much history as is freely available.

Exploratory research only -- not wired to any live account, and NOT the data
source the live strategy would use (that would be Alpaca, like everything
else in this repo). Alpaca's free feed only has ~6 years of history for most
of this universe, too short to include a real bear market other than 2022, so
this pulls from Yahoo Finance's public chart API instead (unauthenticated,
dividend/split-adjusted `adjclose`) purely to get enough history -- back to
2002 -- to see the strategy through 2008 and the 2020/2022 drawdowns, which is
exactly the regime dual momentum's real-world case rests on.

    python backtest_momentum.py
"""

from __future__ import annotations

import json
import statistics
import urllib.request
from datetime import datetime

RISKY = ["SPY", "QQQ", "EFA", "IWM"]
SAFE = "SHY"
UNIVERSE = RISKY + [SAFE]
LOOKBACK_MONTHS = 12  # trailing total-return window used to rank momentum


def fetch_monthly_adjclose(symbol: str) -> dict[str, float]:
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?range=25y&interval=1mo&includeAdjustedClose=true")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    data = json.loads(urllib.request.urlopen(req, timeout=20).read())
    result = data["chart"]["result"][0]
    ts = result["timestamp"]
    adj = result["indicators"]["adjclose"][0]["adjclose"]
    out = {}
    for t, c in zip(ts, adj):
        if c is None:
            continue
        month = datetime.utcfromtimestamp(t).strftime("%Y-%m")
        out[month] = c  # last bar wins if a month appears twice
    return out


def max_drawdown(equity: list[float]) -> float:
    peak = equity[0]
    worst = 0.0
    for v in equity:
        peak = max(peak, v)
        worst = min(worst, (v - peak) / peak)
    return worst


def cagr(equity: list[float], n_months: int) -> float:
    years = n_months / 12.0
    return (equity[-1] / equity[0]) ** (1 / years) - 1 if years > 0 else 0.0


def sharpe(monthly_returns: list[float]) -> float:
    if len(monthly_returns) < 2:
        return 0.0
    mean = statistics.mean(monthly_returns)
    stdev = statistics.pstdev(monthly_returns)
    return (mean / stdev) * (12 ** 0.5) if stdev else 0.0


def simulate_buy_and_hold(series: dict[str, float], months: list[str], start_i: int) -> list[float]:
    eq = [1.0]
    for i in range(start_i, len(months) - 1):
        r = series[months[i + 1]] / series[months[i]] - 1
        eq.append(eq[-1] * (1 + r))
    return eq


def report(label: str, equity: list[float]) -> None:
    n = len(equity) - 1
    rets = [equity[j + 1] / equity[j] - 1 for j in range(n)]
    print(f"{label:24} total={  (equity[-1]-1)*100:7.1f}%  CAGR={cagr(equity, n)*100:6.1f}%  "
          f"MDD={max_drawdown(equity)*100:7.1f}%  Sharpe={sharpe(rets):5.2f}")


def main() -> None:
    print("Fetching monthly history from Yahoo Finance...")
    series = {sym: fetch_monthly_adjclose(sym) for sym in UNIVERSE}
    months = sorted(set.intersection(*(set(s.keys()) for s in series.values())))
    print(f"Common history: {months[0]} .. {months[-1]} ({len(months)} months)\n")

    strat_equity = [1.0]
    strat_monthly_returns: list[float] = []
    holdings_log: list[str] = []

    for i in range(LOOKBACK_MONTHS, len(months) - 1):
        decision_month = months[i]
        next_month = months[i + 1]
        base_month = months[i - LOOKBACK_MONTHS]
        trailing = {sym: series[sym][decision_month] / series[sym][base_month] - 1 for sym in UNIVERSE}
        best_risky = max(RISKY, key=lambda s: trailing[s])
        holding = best_risky if trailing[best_risky] > trailing[SAFE] else SAFE
        holdings_log.append(holding)
        month_return = series[holding][next_month] / series[holding][decision_month] - 1
        strat_equity.append(strat_equity[-1] * (1 + month_return))
        strat_monthly_returns.append(month_return)

    start_i = LOOKBACK_MONTHS
    spy_bh = simulate_buy_and_hold(series["SPY"], months, start_i)
    qqq_bh = simulate_buy_and_hold(series["QQQ"], months, start_i)

    print(f"Simulated {len(strat_equity)-1} monthly rebalances "
          f"({months[LOOKBACK_MONTHS]} .. {months[-1]})\n")
    report("Momentum rotation", strat_equity)
    report("Buy&Hold SPY", spy_bh)
    report("Buy&Hold QQQ", qqq_bh)

    from collections import Counter
    print("\nMonths held per asset:", dict(Counter(holdings_log)))

    # Worst calendar-year drawdown periods, to see how the strategy behaved
    # specifically during 2008 and 2020 vs. just holding SPY.
    print("\n--- 2008 (Sep 2007 - Mar 2009 window) ---")
    for label, eq in [("Momentum", strat_equity), ("SPY B&H", spy_bh)]:
        idx0 = months.index("2007-09") - start_i
        idx1 = months.index("2009-03") - start_i
        if 0 <= idx0 < len(eq) and 0 <= idx1 < len(eq):
            print(f"  {label}: {(eq[idx1]/eq[idx0]-1)*100:.1f}%")


if __name__ == "__main__":
    main()
