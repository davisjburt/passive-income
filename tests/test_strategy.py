"""Unit tests for indicators, signals, and risk math. No network required."""

import numpy as np
import pandas as pd

from bot import risk
from bot.config import StrategyConfig
from bot.data import rsi, sma
from bot.strategy import evaluate


def _frame(prices):
    idx = pd.date_range("2020-01-01", periods=len(prices), freq="D")
    return pd.DataFrame({"close": prices}, index=idx)


# ---- indicators ----

def test_sma_basic():
    s = pd.Series([1, 2, 3, 4, 5], dtype=float)
    assert sma(s, 5).iloc[-1] == 3.0


def test_rsi_all_gains_is_100():
    s = pd.Series(np.arange(1, 20), dtype=float)
    assert rsi(s, 2).iloc[-1] == 100.0


def test_rsi_all_losses_is_0():
    s = pd.Series(np.arange(20, 1, -1), dtype=float)
    assert rsi(s, 2).iloc[-1] == 0.0


# ---- signals ----

def test_buy_when_oversold_and_above_trend():
    scfg = StrategyConfig()
    # Long steady uptrend (keeps price above 200d SMA), then a sharp dip at the end.
    prices = list(np.linspace(100, 300, 250)) + [250.0]  # final bar dips hard
    sig = evaluate("SPY", _frame(prices), scfg, holding=False)
    assert sig.action == "buy"
    assert sig.rsi <= scfg.rsi_entry


def test_no_buy_below_trend():
    scfg = StrategyConfig()
    # Long downtrend -> price below 200d SMA -> never buy the dip.
    prices = list(np.linspace(300, 100, 250)) + [95.0]
    sig = evaluate("SPY", _frame(prices), scfg, holding=False)
    assert sig.action == "hold"


def test_insufficient_history_holds():
    scfg = StrategyConfig()
    sig = evaluate("SPY", _frame([100, 101, 102]), scfg, holding=False)
    assert sig.action == "hold"
    assert sig.reason == "insufficient history"


def test_sell_when_recovered():
    scfg = StrategyConfig()
    # Uptrend with a strong final push up -> high RSI -> exit a holding.
    prices = list(np.linspace(100, 280, 250)) + [300.0]
    sig = evaluate("SPY", _frame(prices), scfg, holding=True)
    assert sig.action == "sell"


# ---- risk ----

def test_daily_loss_halt():
    assert risk.daily_loss_halt(96.0, 100.0, 0.04) is True
    assert risk.daily_loss_halt(97.0, 100.0, 0.04) is False
    assert risk.daily_loss_halt(100.0, 0.0, 0.04) is False


def test_deployable_cash_respects_buffer_and_no_margin():
    # equity 100, buffer 25 -> may spend down to 25 in cash.
    assert risk.deployable_cash(100, 100, 0.25) == 75.0
    # cash less than buffer -> nothing deployable.
    assert risk.deployable_cash(100, 20, 0.25) == 0.0
    # never exceeds actual cash (no margin).
    assert risk.deployable_cash(100, 50, 0.25) == 25.0


def test_position_notional():
    assert risk.position_notional(1000, 0.20) == 200.0


def test_stop_loss():
    assert risk.hit_stop_loss(100, 91, 0.08) is True
    assert risk.hit_stop_loss(100, 93, 0.08) is False
