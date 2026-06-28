"""Signal generation: RSI(2) mean reversion with a 200-day trend filter."""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from .config import StrategyConfig
from .data import rsi, sma


@dataclass
class Signal:
    symbol: str
    action: str  # "buy" | "sell" | "hold"
    price: float
    rsi: float
    reason: str


def evaluate(symbol: str, df: pd.DataFrame, scfg: StrategyConfig, holding: bool) -> Signal:
    """Decide buy/sell/hold for one symbol given its daily-bar history."""
    close = df["close"]
    price = float(close.iloc[-1])

    if len(close) < scfg.trend_sma + 1:
        return Signal(symbol, "hold", price, math.nan, "insufficient history")

    r = float(rsi(close, scfg.rsi_period).iloc[-1])
    trend = float(sma(close, scfg.trend_sma).iloc[-1])
    exit_ma = float(sma(close, scfg.exit_sma).iloc[-1])

    if holding:
        # Exit when the bounce has happened: RSI recovered or price back above
        # the short-term average. (Stop-loss and time-stop are handled in trader.)
        if r >= scfg.rsi_exit:
            return Signal(symbol, "sell", price, r, f"rsi recovered {r:.1f}")
        if price > exit_ma:
            return Signal(symbol, "sell", price, r, f"price>{scfg.exit_sma}d sma")
        return Signal(symbol, "hold", price, r, f"holding rsi={r:.1f}")

    # Entry: deeply oversold but still in a long-term uptrend.
    if price > trend and r <= scfg.rsi_entry:
        return Signal(symbol, "buy", price, r, f"oversold rsi={r:.1f}, above 200d")
    return Signal(symbol, "hold", price, r, "no setup")
