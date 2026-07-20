"""Unit tests for the pure wheel logic (no network)."""

from datetime import date, timedelta

from bot.wheel.strategy import (
    Contract,
    LegRules,
    WheelState,
    aggregate_premium,
    annualized_yield,
    call_strike_band,
    is_suspicious_early_close,
    max_contracts,
    parse_occ,
    per_stock_ok,
    portfolio_ok,
    put_strike_band,
    reconstruct_state,
    select_call,
    select_put,
    should_roll_call,
    should_roll_put,
)

TODAY = date(2026, 1, 1)


# ---- state machine ----

def test_reconstruct_state():
    assert reconstruct_state("put", 0) == WheelState.PUT_OPEN
    assert reconstruct_state("call", 100) == WheelState.CALL_OPEN
    assert reconstruct_state(None, 100) == WheelState.LONG_STOCK
    assert reconstruct_state(None, 0) == WheelState.CASH
    assert reconstruct_state(None, 50) == WheelState.CASH  # <100 shares isn't a wheel lot


# ---- stale-data guard ----

def test_suspicious_early_close_flags_premature_cash():
    # exp is 24 days out -- vanishing to CASH today is not a real expiration.
    assert is_suspicious_early_close("PUT_OPEN", "CASH", date(2026, 1, 25), TODAY) is True
    assert is_suspicious_early_close("CALL_OPEN", "CASH", date(2026, 1, 25), TODAY) is True


def test_suspicious_early_close_allows_real_expiration():
    # today is on/after (exp - buffer) -> a legitimate expiration, not suspicious.
    assert is_suspicious_early_close("PUT_OPEN", "CASH", date(2026, 1, 2), TODAY) is False
    assert is_suspicious_early_close("PUT_OPEN", "CASH", date(2026, 1, 1), TODAY) is False


def test_suspicious_early_close_ignores_non_cash_or_non_open_transitions():
    assert is_suspicious_early_close("PUT_OPEN", "LONG_STOCK", date(2026, 1, 25), TODAY) is False
    assert is_suspicious_early_close("CASH", "PUT_OPEN", None, TODAY) is False
    assert is_suspicious_early_close("LONG_STOCK", "CASH", date(2026, 1, 25), TODAY) is False


def test_suspicious_early_close_requires_known_expiration():
    assert is_suspicious_early_close("PUT_OPEN", "CASH", None, TODAY) is False


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


def test_max_contracts_limited_by_per_stock_cap():
    # equity $10k, strike $18 -> $1,800/contract. 25% per-stock cap = $2,500 -> 1 fits.
    assert max_contracts(10_000, 18.0, 0.25, exposure=0, portfolio_cap_pct=0.70) == 1
    # cheaper strike ($12) -> $1,200/contract, $2,500 cap -> 2 fit.
    assert max_contracts(10_000, 12.0, 0.25, exposure=0, portfolio_cap_pct=0.70) == 2


def test_max_contracts_limited_by_portfolio_cap():
    # per-stock cap alone would allow 2 ($1,200 x 2 = $2,400 < $2,500), but only
    # $1,300 of portfolio room remains -> just 1 fits.
    assert max_contracts(10_000, 12.0, 0.25, exposure=5700, portfolio_cap_pct=0.70) == 1


def test_max_contracts_zero_when_no_room():
    assert max_contracts(10_000, 12.0, 0.25, exposure=7000, portfolio_cap_pct=0.70) == 0
    assert max_contracts(0, 12.0, 0.25, exposure=0, portfolio_cap_pct=0.70) == 0
    assert max_contracts(10_000, 0, 0.25, exposure=0, portfolio_cap_pct=0.70) == 0


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


def test_aggregate_premium():
    fills = [
        {"underlying": "CLF", "side": "sell", "credit": 21.0},   # sold a put, +$21
        {"underlying": "CLF", "side": "buy", "credit": 5.0},     # bought back, -$5
        {"underlying": "F", "side": "sell", "credit": 30.0},     # sold a put, +$30
    ]
    agg = aggregate_premium(fills)
    assert agg["CLF"]["gross_premium"] == 21.0
    assert agg["CLF"]["debits"] == 5.0
    assert agg["CLF"]["realized"] == 16.0   # 21 - 5
    assert agg["F"]["gross_premium"] == 30.0
    assert agg["F"]["realized"] == 30.0     # expired worthless -> keep full credit


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


# ---- roll triggers ----

def test_roll_trigger_put():
    # Roll when spot is within 5% OTM (approaching strike from above) or ITM.
    assert should_roll_put(105.0, 100.0, 0.05) is True   # exactly 5% OTM — boundary
    assert should_roll_put(104.9, 100.0, 0.05) is True   # just inside 5% buffer
    assert should_roll_put(98.0, 100.0, 0.05) is True    # ITM → definitely roll
    assert should_roll_put(105.1, 100.0, 0.05) is False  # 5.1% OTM — safe, no roll


def test_roll_trigger_call():
    # Roll when spot is within 3% OTM (approaching call strike from below) or ITM.
    assert should_roll_call(97.0, 100.0, 0.03) is True   # exactly 3% OTM — boundary
    assert should_roll_call(97.1, 100.0, 0.03) is True   # just inside 3% buffer
    assert should_roll_call(103.0, 100.0, 0.03) is True  # ITM → definitely roll
    assert should_roll_call(96.9, 100.0, 0.03) is False  # 3.1% OTM — safe, no roll


def test_aggregate_premium_debit_can_produce_negative_realized():
    fills = [
        {"underlying": "MARA", "side": "sell", "credit": 50.0},
        {"underlying": "MARA", "side": "buy", "credit": 80.0},  # roll cost more than premium
    ]
    agg = aggregate_premium(fills)
    assert agg["MARA"]["realized"] == -30.0  # net loss from rolling
    assert agg["MARA"]["gross_premium"] == 50.0
    assert agg["MARA"]["debits"] == 80.0


# ---- notifications ----

def test_telegram_disabled_when_env_unset(monkeypatch):
    from bot import notify
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert notify.telegram_enabled() is False
    assert notify.send_telegram("hi") is False
    assert notify.notify_trades(["BUY SPY ~$100"]) is False


def test_notify_trades_empty_is_noop(monkeypatch):
    from bot import notify
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "y")
    assert notify.notify_trades([]) is False


def test_alert_returns_true_on_successful_send(monkeypatch):
    from bot.wheel import engine
    monkeypatch.setattr("bot.notify.send_telegram", lambda text: True)
    assert engine._alert("hi") is True


def test_alert_returns_false_on_failed_send(monkeypatch):
    """A dropped Telegram send must be reported back, not swallowed -- callers
    rely on this to avoid marking a daily notification as sent when it wasn't."""
    from bot.wheel import engine
    monkeypatch.setattr("bot.notify.send_telegram", lambda text: False)
    assert engine._alert("hi") is False


def test_alert_returns_false_on_exception(monkeypatch):
    from bot.wheel import engine
    def boom(text):
        raise RuntimeError("network exploded")
    monkeypatch.setattr("bot.notify.send_telegram", boom)
    assert engine._alert("hi") is False


# ---- EOD summary timing ----

def test_near_market_close_true_in_evening_window():
    from bot.wheel.engine import _near_market_close
    from datetime import datetime, timezone
    assert _near_market_close(datetime(2026, 7, 20, 20, 0, tzinfo=timezone.utc)) is True
    assert _near_market_close(datetime(2026, 7, 20, 21, 55, tzinfo=timezone.utc)) is True


def test_near_market_close_false_before_market_open():
    """This is the guard against the reported bug: a pre-market tick the
    morning after a missed evening cycle must not fire a stale EOD summary."""
    from bot.wheel.engine import _near_market_close
    from datetime import datetime, timezone
    assert _near_market_close(datetime(2026, 7, 20, 13, 0, tzinfo=timezone.utc)) is False
    assert _near_market_close(datetime(2026, 7, 20, 19, 59, tzinfo=timezone.utc)) is False
