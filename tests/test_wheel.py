"""Unit tests for the pure wheel logic (no network)."""

from datetime import date, timedelta

from bot.wheel.strategy import (
    Contract,
    LegRules,
    WheelState,
    annualized_yield,
    call_strike_band,
    parse_occ,
    per_stock_ok,
    portfolio_ok,
    put_strike_band,
    reconstruct_state,
    select_call,
    select_put,
)

TODAY = date(2026, 1, 1)


# ---- state machine ----

def test_reconstruct_state():
    assert reconstruct_state("put", 0) == WheelState.PUT_OPEN
    assert reconstruct_state("call", 100) == WheelState.CALL_OPEN
    assert reconstruct_state(None, 100) == WheelState.LONG_STOCK
    assert reconstruct_state(None, 0) == WheelState.CASH
    assert reconstruct_state(None, 50) == WheelState.CASH  # <100 shares isn't a wheel lot


# ---- OCC parsing ----

def test_parse_occ():
    under, exp, typ, strike = parse_occ("AAPL250117P00150000")
    assert under == "AAPL" and typ == "put" and strike == 150.0
    assert exp == date(2025, 1, 17)
    assert parse_occ("SPY260116C00500000")[2] == "call"


# ---- yield ----

def test_annualized_yield():
    # $200 premium on $15,000 reserved, 35 DTE -> ~13.9%
    y = annualized_yield(200, 15_000, 35)
    assert 0.13 < y < 0.15
    assert annualized_yield(100, 0, 30) == 0.0
    assert annualized_yield(100, 1000, 0) == 0.0


# ---- bands ----

def test_put_band_is_below_spot():
    lo, hi = put_strike_band(100, (0.10, 0.15))
    assert lo == 85.0 and hi == 90.0


def test_call_band_is_above_reference():
    lo, hi = call_strike_band(100, (0.05, 0.10))
    assert round(lo, 6) == 105.0 and round(hi, 6) == 110.0


# ---- caps ----

def test_caps():
    assert per_stock_ok(8000, 100_000, 0.08) is True
    assert per_stock_ok(9000, 100_000, 0.08) is False
    assert portfolio_ok(40_000, 5_000, 100_000, 0.45) is True
    assert portfolio_ok(43_000, 5_000, 100_000, 0.45) is False


# ---- selection ----

def _put(strike, bid, dte_days):
    return Contract("X", "X", "put", strike, TODAY + timedelta(days=dte_days), bid)


def test_select_put_picks_highest_yield_within_caps():
    rules = LegRules(band=(0.10, 0.15), dte=(30, 45), min_annual_yield=0.10)
    spot = 100.0  # band 85-90
    cands = [
        _put(88, 1.50, 35),   # ~17.7% ann, in band -> eligible
        _put(90, 1.00, 35),   # ~11.6% ann, in band -> eligible (lower yield)
        _put(80, 2.00, 35),   # out of band (too low) -> rejected
        _put(88, 0.20, 35),   # ~2.4% ann -> below min yield
    ]
    pick = select_put(cands, spot, rules, TODAY, equity=1_000_000,
                      per_stock_cap_pct=0.10, exposure=0, portfolio_cap_pct=0.45)
    assert pick is not None
    chosen, y = pick
    assert chosen.strike == 88 and chosen.bid == 1.50  # highest-yield eligible


def test_select_put_respects_per_stock_cap():
    rules = LegRules(band=(0.10, 0.15), dte=(30, 45), min_annual_yield=0.05)
    spot = 100.0
    cands = [_put(88, 1.50, 35)]  # reserve = 88*100 = 8800
    # cap 8% of 100k = 8000 < 8800 -> rejected
    assert select_put(cands, spot, rules, TODAY, 100_000, 0.08, 0, 0.45) is None
    # cap 10% = 10000 -> ok
    assert select_put(cands, spot, rules, TODAY, 100_000, 0.10, 0, 0.45) is not None


def test_select_call_anchors_above_price_when_underwater():
    rules = LegRules(band=(0.05, 0.10), dte=(30, 45), min_annual_yield=0.05)
    # basis 100 but price ran to 120 -> ref=120, band 126-132 (not 105-110)
    exp = TODAY + timedelta(days=35)
    cands = [
        Contract("X", "X", "call", 128, exp, 2.0),  # in 126-132 band
        Contract("X", "X", "call", 108, exp, 5.0),  # below ref band -> rejected
    ]
    pick = select_call(cands, basis=100, current_price=120, rules=rules, today=TODAY)
    assert pick is not None and pick[0].strike == 128
