"""Market data fetching and technical indicators."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

_TIMEFRAMES: dict[str, TimeFrame] = {
    "1Min":  TimeFrame(1,  TimeFrameUnit.Minute),
    "5Min":  TimeFrame(5,  TimeFrameUnit.Minute),
    "15Min": TimeFrame(15, TimeFrameUnit.Minute),
    "30Min": TimeFrame(30, TimeFrameUnit.Minute),
    "1Hour": TimeFrame.Hour,
    "Day":   TimeFrame.Day,
}


def get_bars(
    data_client: StockHistoricalDataClient,
    symbols: list[str],
    lookback_days: int = 30,
    timeframe: str = "Day",
) -> dict[str, pd.DataFrame]:
    """Return {symbol: DataFrame of bars sorted oldest->newest}."""
    tf = _TIMEFRAMES.get(timeframe, TimeFrame.Day)
    start = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    req = StockBarsRequest(
        symbol_or_symbols=list(symbols),
        timeframe=tf,
        start=start,
    )
    resp = data_client.get_stock_bars(req)
    df = resp.df
    out: dict[str, pd.DataFrame] = {}
    if df is None or df.empty:
        return out
    available = set(df.index.get_level_values(0))
    for sym in symbols:
        if sym in available:
            out[sym] = df.xs(sym).sort_index()
    return out


def get_daily_bars(
    data_client: StockHistoricalDataClient,
    symbols: list[str],
    lookback_days: int = 400,
) -> dict[str, pd.DataFrame]:
    return get_bars(data_client, symbols, lookback_days, "Day")


def rsi(series: pd.Series, period: int) -> pd.Series:
    """Wilder's RSI."""
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    out = 100 - (100 / (1 + rs))
    # Edge cases: all gains -> RSI 100; all losses -> RSI 0.
    out = out.mask(avg_loss == 0, 100.0)
    out = out.mask(avg_gain == 0, 0.0)
    return out


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()
