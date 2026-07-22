"""Unit tests for the pure momentum logic (no network)."""

import pytest

from bot.momentum.strategy import choose_holding, trailing_return


# ---- trailing_return ----

def test_trailing_return_positive():
    assert trailing_return(price_now=110, price_then=100) == pytest.approx(0.1)


def test_trailing_return_negative():
    assert trailing_return(price_now=80, price_then=100) == pytest.approx(-0.2)


def test_trailing_return_zero_base_is_safe():
    assert trailing_return(price_now=100, price_then=0) == 0.0


# ---- choose_holding ----

def test_choose_holding_picks_best_risky_asset_when_it_beats_safe():
    risky = {"SPY": 0.10, "QQQ": 0.18, "EFA": 0.05, "IWM": 0.02}
    assert choose_holding(risky, "SHY", safe_return=0.03) == "QQQ"


def test_choose_holding_falls_back_to_safe_when_best_risky_still_loses():
    """The absolute-momentum filter: even the best-performing risky asset
    isn't good enough if it's behind the safe asset -- e.g. every risky asset
    is negative during a broad bear market, so sit it out in the safe asset
    rather than picking "least bad"."""
    risky = {"SPY": -0.15, "QQQ": -0.22, "EFA": -0.30, "IWM": -0.18}
    assert choose_holding(risky, "SHY", safe_return=0.02) == "SHY"


def test_choose_holding_empty_universe_returns_safe():
    assert choose_holding({}, "SHY", safe_return=0.01) == "SHY"


def test_choose_holding_boundary_tie_goes_to_safe():
    """Strictly greater-than, not >=: a risky asset that exactly ties the
    safe asset doesn't get chosen -- only real outperformance does."""
    risky = {"SPY": 0.05}
    assert choose_holding(risky, "SHY", safe_return=0.05) == "SHY"


# ---- state persistence ----

def test_state_round_trips(tmp_path, monkeypatch):
    from bot.momentum import engine
    monkeypatch.setattr(engine, "DOCS_DIR", tmp_path)
    assert engine.load_state("test") == {}
    engine.save_state({"last_rebalance_month": "2026-07", "current_holding": "QQQ"}, "test")
    assert engine.load_state("test") == {"last_rebalance_month": "2026-07", "current_holding": "QQQ"}


def test_state_path_is_account_specific(tmp_path, monkeypatch):
    from bot.momentum import engine
    monkeypatch.setattr(engine, "DOCS_DIR", tmp_path)
    assert engine.state_path("momentum").name == "momentum_state_momentum.json"
    assert engine.state_path("other").name == "momentum_state_other.json"


# ---- _current_holding ----

class _FakePosition:
    def __init__(self, symbol, qty):
        self.symbol = symbol
        self.qty = qty


class _FakeTradingClient:
    def __init__(self, positions):
        self._positions = positions

    def get_all_positions(self):
        return self._positions


def test_current_holding_returns_the_held_universe_symbol():
    from bot.momentum.engine import _current_holding
    trading = _FakeTradingClient([_FakePosition("QQQ", "10.5")])
    assert _current_holding(trading, ["SPY", "QQQ", "EFA", "IWM", "SHY"]) == "QQQ"


def test_current_holding_none_when_flat():
    from bot.momentum.engine import _current_holding
    trading = _FakeTradingClient([])
    assert _current_holding(trading, ["SPY", "QQQ", "EFA", "IWM", "SHY"]) is None


def test_current_holding_ignores_positions_outside_the_universe():
    from bot.momentum.engine import _current_holding
    trading = _FakeTradingClient([_FakePosition("AAPL", "5")])
    assert _current_holding(trading, ["SPY", "QQQ", "EFA", "IWM", "SHY"]) is None


# ---- report file path ----

def test_momentum_file_path_is_account_specific():
    from bot.momentum.report import momentum_file_path
    assert momentum_file_path("momentum").name == "momentum_momentum.json"
    assert momentum_file_path("other").name == "momentum_other.json"
