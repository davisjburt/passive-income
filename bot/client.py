"""Thin factory helpers for Alpaca SDK clients."""

from __future__ import annotations

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.trading.client import TradingClient

from .config import Config


def make_trading_client(cfg: Config) -> TradingClient:
    # paper=cfg.paper is always True (see Config) — guards against live trading.
    return TradingClient(cfg.api_key, cfg.api_secret, paper=cfg.paper)


def make_data_client(cfg: Config) -> StockHistoricalDataClient:
    return StockHistoricalDataClient(cfg.api_key, cfg.api_secret)
