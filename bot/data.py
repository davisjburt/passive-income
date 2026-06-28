"""Market data fetching and technical indicators."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame


def get_daily_bars(
    data_client: StockHistoricalDataClient,
    symbols: list[str],
    lookback_days: int = 400,
) -> dict[str, pd.DataFrame]:
    """Return {symbol: DataFrame of daily bars sorted oldest->newest}."""
    start = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    req = StockBarsRequest(
        symbol_or_symbols=list(symbols),
        timeframe=TimeFrame.Day,
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
