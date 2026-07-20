"""Wheel engine: broker I/O, per-symbol ledger, and the once-per-day cycle.

State is reconstructed from broker positions each run (stateless-CI safe). The
ledger (docs/wheel_ledger.json) only tracks things the broker doesn't hand us
cleanly: cumulative premium collected, effective cost basis, and entry price for
the drawdown safeguard.

NOTE: the option-data and order-placement paths require Alpaca options Level 1 and
have not been exercised live yet — run with --dry-run first and confirm the logged
contracts/quotes look right before removing it.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionChainRequest, StockLatestQuoteRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import ContractType, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, LimitOrderRequest

from .config import WheelConfig
from .strategy import (
    Contract,
    WheelState,
    annualized_yield,
    call_strike_band,
    is_suspicious_early_close,
    max_contracts,
    parse_occ,
    put_strike_band,
    reconstruct_state,
    select_call,
    select_put,
    should_roll_call,
    should_roll_put,
)

log = logging.getLogger("wheel")
DOCS_DIR = Path(__file__).resolve().parents[2] / "docs"


def ledger_path(account: str = "default") -> Path:
    suffix = "" if account == "default" else f"_{account}"
    return DOCS_DIR / f"wheel_ledger{suffix}.json"

ROLL_TRIGGER_PUT  = 0.05  # roll put when spot ≤ strike × 1.05 (5% ITM buffer)
ROLL_TRIGGER_CALL = 0.03  # roll call when spot ≥ strike × 0.97 (3% ITM buffer)
MAX_ROLLS         = 3
MIN_DTE_TO_ROLL   = 7


# ---------------- ledger ----------------

@dataclass
class SymbolLedger:
    premium_collected: float = 0.0
    realized_pnl: float = 0.0
    cost_basis: float = 0.0   # set on assignment = strike - put premium/share
    entry_price: float = 0.0  # underlying price when the wheel started (for drawdown)
    rolls: int = 0             # number of times this position has been rolled (capped at MAX_ROLLS)
    last_state: str = ""       # previous WheelState value — used to detect transitions
    last_exp: str = ""         # ISO date of the last tracked open option's expiration —
                               # used to catch a stale/incomplete positions fetch that
                               # would otherwise look like a legitimate early close
    drawdown_halt_alerted: bool = False  # prevents repeated drawdown-halt notifications


class Ledger:
    def __init__(self, data: dict | None = None, path: Path | None = None):
        raw = data or {}
        self.path = path or ledger_path("default")
        self.meta: dict = raw.get("_meta", {})  # cross-run state: notification dates
        self.data: dict[str, SymbolLedger] = {
            k: SymbolLedger(**v) for k, v in raw.items() if k != "_meta"
        }

    @classmethod
    def load(cls, account: str = "default") -> "Ledger":
        path = ledger_path(account)
        if path.exists():
            try:
                return cls(json.loads(path.read_text()), path=path)
            except json.JSONDecodeError:
                pass
        return cls(path=path)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        out: dict = {k: vars(v) for k, v in self.data.items()}
        if self.meta:
            out["_meta"] = self.meta
        self.path.write_text(json.dumps(out, indent=2))

    def get(self, sym: str) -> SymbolLedger:
        return self.data.setdefault(sym, SymbolLedger())

    def drawdown(self, sym: str, current_price: float) -> float:
        e = self.get(sym).entry_price
        return (e - current_price) / e if e > 0 else 0.0


def _with_retries(fn, *args, retries: int = 3, what: str = "broker call", **kwargs):
    """Retry a broker read call with backoff on transient errors (e.g. Alpaca
    gateway timeouts). Mirrors the retry pattern already used for Telegram sends
    in bot.notify -- a brief blip shouldn't fail an entire 5-minute cycle when
    the next attempt a couple seconds later would likely succeed.
    """
    for attempt in range(1, retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            if attempt == retries:
                raise
            log.warning("%s failed (attempt %d/%d): %s -- retrying", what, attempt, retries, exc)
            time.sleep(2 ** attempt)  # 2s, 4s


# ---------------- broker views ----------------

@dataclass
class PositionsView:
    shares: dict = field(default_factory=dict)   # sym -> {qty, price, basis}
    options: dict = field(default_factory=dict)  # underlying -> {type, occ, strike, exp, qty}

    def exposure(self) -> float:
        share_notional = sum(p["qty"] * p["price"] for p in self.shares.values())
        put_cash = sum(o["strike"] * 100 * o.get("qty", 1) for o in self.options.values() if o["type"] == "put")
        return share_notional + put_cash

    def wheel_names(self) -> set[str]:
        return set(self.shares) | set(self.options)


def build_positions_view(trading: TradingClient) -> PositionsView:
    pv = PositionsView()
    for p in _with_retries(trading.get_all_positions, what="get_all_positions"):
        ac = getattr(p.asset_class, "value", str(p.asset_class))
        if ac == "us_option":
            try:
                under, exp, typ, strike = parse_occ(p.symbol)
            except ValueError:
                continue
            pv.options[under] = {
                "type": typ, "occ": p.symbol, "strike": strike, "exp": exp,
                "qty": abs(int(float(p.qty))),
            }
        else:  # us_equity
            pv.shares[p.symbol] = {
                "qty": float(p.qty),
                "price": float(p.current_price or 0),
                "basis": float(p.avg_entry_price or 0),
            }
    return pv


def pending_option_underlyings(trading: TradingClient) -> dict:
    """Underlyings that already have an OPEN option order, so we don't duplicate.

    A resting sell-to-open order has no position yet, so reconstruct_state would
    otherwise see CASH/LONG_STOCK and fire again. Critical once we run frequently.
    """
    out: dict[str, dict] = {}
    try:
        orders = trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
    except Exception as exc:  # noqa: BLE001
        log.warning("open-orders fetch failed: %s", exc)
        return out
    for o in orders:
        try:
            under, _exp, typ, strike = parse_occ(o.symbol)
        except ValueError:
            continue  # equity order, not an option
        out[under] = {"type": typ, "strike": strike, "qty": abs(int(float(o.qty)))}
    return out


def get_spot(data: StockHistoricalDataClient, symbol: str) -> float | None:
    try:
        q = data.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=symbol))
        qq = q[symbol]
        bid, ask = float(qq.bid_price or 0), float(qq.ask_price or 0)
        if bid and ask:
            return (bid + ask) / 2
        return bid or ask or None
    except Exception as exc:  # noqa: BLE001
        log.warning("spot quote failed for %s: %s", symbol, exc)
        return None


def fetch_contracts(opt: OptionHistoricalDataClient, underlying: str, ctype: str,
                    strike_lo: float, strike_hi: float, dte: tuple[int, int],
                    today: date) -> list[Contract]:
    req = OptionChainRequest(
        underlying_symbol=underlying,
        feed="indicative",  # free feed; switch to "opra" if you have that data plan
        type=ContractType.PUT if ctype == "put" else ContractType.CALL,
        strike_price_gte=round(strike_lo, 2),
        strike_price_lte=round(strike_hi, 2),
        expiration_date_gte=(today + timedelta(days=dte[0])).isoformat(),
        expiration_date_lte=(today + timedelta(days=dte[1])).isoformat(),
    )
    out: list[Contract] = []
    try:
        chain = opt.get_option_chain(req)
    except Exception as exc:  # noqa: BLE001
        log.warning("option chain failed for %s %s: %s", underlying, ctype, exc)
        return out
    for occ, snap in (chain or {}).items():
        try:
            under, exp, typ, strike = parse_occ(occ)
        except ValueError:
            continue
        quote = getattr(snap, "latest_quote", None)
        bid = float(getattr(quote, "bid_price", 0) or 0) if quote else 0.0
        ask = float(getattr(quote, "ask_price", 0) or 0) if quote else 0.0
        out.append(Contract(occ, under, typ, strike, exp, bid, ask))
    return out


# ---------------- cycle ----------------

def run_wheel_cycle(cfg: WheelConfig, dry_run: bool = True) -> dict:
    trading = TradingClient(cfg.api_key, cfg.api_secret, paper=cfg.paper)
    data = StockHistoricalDataClient(cfg.api_key, cfg.api_secret)
    opt = OptionHistoricalDataClient(cfg.api_key, cfg.api_secret)
    ledger = Ledger.load(cfg.account)

    summary = {"actions": [], "skipped": [], "metrics": {}}

    if not cfg.enabled and not dry_run:
        log.warning("wheel disabled (config.wheel.yaml enabled:false). Nothing to do.")
        return summary

    clock = _with_retries(trading.get_clock, what="get_clock")
    acct = _with_retries(trading.get_account, what="get_account")
    equity = float(acct.equity)
    summary["market_open"] = clock.is_open

    if not clock.is_open and not dry_run:
        log.info("Market closed. Skipping.")
        # EOD summary: fire once per day, only near actual market close (see
        # _near_market_close) and only marked done if the send actually succeeded
        # -- a failed send retries on the next cycle instead of being lost.
        now_utc = datetime.now(timezone.utc)
        today_str = now_utc.date().isoformat()
        if _near_market_close(now_utc) and ledger.meta.get("eod_date") != today_str:
            pv_eod = build_positions_view(trading)
            total_prem = sum(
                getattr(ledger.get(s), "premium_collected", 0.0)
                for s in ledger.data
            )
            total_pnl = sum(
                getattr(ledger.get(s), "realized_pnl", 0.0)
                for s in ledger.data
            )
            if _send_eod_summary(pv_eod, equity, total_prem, total_pnl, cfg.account):
                ledger.meta["eod_date"] = today_str
                ledger.save()
        return summary

    pv = build_positions_view(trading)
    pending = pending_option_underlyings(trading)
    # Count pending sell-to-open puts toward exposure + active names so caps and
    # the max-tickers limit hold even before orders fill.
    exposure = pv.exposure() + sum(
        p["strike"] * 100 * p.get("qty", 1) for p in pending.values() if p["type"] == "put")
    active = len(pv.wheel_names() | set(pending))
    today = datetime.now(timezone.utc).date()

    log.info("equity=$%.0f exposure=$%.0f (%.0f%%) names=%d",
             equity, exposure, exposure / equity * 100 if equity else 0, active)

    # Morning briefing: once per trading day, on the first market-open run.
    # Only marked done if the send succeeded, same reasoning as the EOD summary.
    if not dry_run:
        today_str = today.isoformat()
        if ledger.meta.get("morning_date") != today_str:
            if _send_morning_briefing(pv, equity, exposure, cfg):
                ledger.meta["morning_date"] = today_str

    # Extend the loop to cover positions that exist in the broker but have been
    # removed from the configured universe (e.g. switching from aggressive to
    # conservative universe). These are managed in roll-only mode: the bot rolls
    # them if near ITM but never opens fresh positions on them.
    universe_set = set(cfg.universe)
    orphaned = sorted(pv.wheel_names() - universe_set)
    if orphaned:
        log.info("Orphaned positions (roll-only, outside universe): %s", orphaned)

    for sym in list(cfg.universe) + orphaned:
        in_universe = sym in universe_set
        if sym in pending:
            summary["skipped"].append(f"{sym}: order already pending")
            continue
        opt_type = pv.options.get(sym, {}).get("type")
        share_qty = pv.shares.get(sym, {}).get("qty", 0.0)
        state = reconstruct_state(opt_type, share_qty)

        led = ledger.get(sym)
        old_state = led.last_state
        old_exp = None
        if led.last_exp:
            try:
                old_exp = date.fromisoformat(led.last_exp)
            except ValueError:
                old_exp = None

        # Guard against a stale/incomplete positions fetch masquerading as a real
        # close (this is exactly how a duplicate-position incident happened: the
        # broker briefly looked empty, the bot read that as CASH, and sold a
        # fresh put on top of one still open). Skip this symbol entirely rather
        # than trust an implausibly early close.
        if is_suspicious_early_close(old_state, state.value, old_exp, today):
            log.warning("%s: %s -> CASH looks premature vs tracked expiration %s "
                       "-- likely a bad positions fetch. Skipping this cycle.",
                       sym, old_state, led.last_exp)
            if not dry_run:
                _alert(
                    f"⚠️ *{sym} — suspicious state change*\n"
                    f"Was {old_state} (tracked exp {led.last_exp}); broker now shows "
                    f"CASH well before that date with no roll recorded. Skipping "
                    f"{sym} this cycle rather than risk selling a duplicate put — "
                    f"will re-check next cycle.",
                    cfg.account,
                )
            summary["skipped"].append(f"{sym}: suspicious early close vs tracked expiration, skipped")
            continue

        # Detect and notify state transitions (e.g. assignment, expiry, called away).
        if not dry_run and old_state and old_state != state.value:
            _notify_transition(sym, old_state, state.value, led, cfg.account)

        if state == WheelState.CASH:
            if not in_universe:
                continue  # orphaned symbol returned to cash — don't open new positions
            if active >= cfg.max_wheel_tickers:
                summary["skipped"].append(f"{sym}: at max wheel names"); continue
            spot = get_spot(data, sym)
            if not spot:
                summary["skipped"].append(f"{sym}: no quote"); continue
            if ledger.drawdown(sym, spot) >= cfg.safeguards.halt_new_puts_drawdown_pct:
                if not dry_run and not ledger.get(sym).drawdown_halt_alerted:
                    dd_pct = ledger.drawdown(sym, spot) * 100
                    if _alert(
                        f"⚠️ *{sym} — drawdown halt triggered*\n"
                        f"Down {dd_pct:.1f}% from entry — pausing new puts until it recovers",
                        cfg.account,
                    ):
                        ledger.get(sym).drawdown_halt_alerted = True
                summary["skipped"].append(f"{sym}: drawdown halt"); continue
            ledger.get(sym).drawdown_halt_alerted = False  # reset when no longer halted
            lo, hi = put_strike_band(spot, cfg.put.band)
            cands = fetch_contracts(opt, sym, "put", lo, hi, cfg.put.dte, today)
            pick = select_put(cands, spot, cfg.put, today, equity,
                              cfg.per_stock_cap_pct, exposure, cfg.portfolio_wheel_cap_pct)
            if not pick:
                summary["skipped"].append(f"{sym}: no put meets filters"); continue
            c, y = pick
            qty = max(1, max_contracts(equity, c.strike, cfg.per_stock_cap_pct,
                                       exposure, cfg.portfolio_wheel_cap_pct))
            _sell_to_open(trading, c, y, cfg, dry_run, summary, qty=qty)
            if not dry_run and not ledger.get(sym).entry_price:
                # Reference price for the drawdown circuit-breaker on future puts.
                ledger.get(sym).entry_price = spot
            exposure += c.strike * 100 * qty
            active += 1

        elif state == WheelState.LONG_STOCK:
            if not in_universe:
                continue  # orphaned symbol holding shares — don't sell calls, let expire/manual
            shr = pv.shares[sym]
            basis = ledger.get(sym).cost_basis or shr["basis"]
            cands = fetch_contracts(
                opt, sym, "call",
                *call_strike_band(max(basis, shr["price"]), cfg.call.band),
                cfg.call.dte, today)
            pick = select_call(cands, basis, shr["price"], cfg.call, today)
            if not pick:
                summary["skipped"].append(f"{sym}: no call meets filters"); continue
            c, y = pick
            call_qty = max(1, int(shr["qty"] // 100))  # one call per 100 shares held
            _sell_to_open(trading, c, y, cfg, dry_run, summary, qty=call_qty)

        elif state == WheelState.PUT_OPEN:
            opt_info = pv.options.get(sym, {})
            occ = opt_info.get("occ", "")
            put_strike = opt_info.get("strike", 0.0)
            put_qty = opt_info.get("qty", 1)
            exp = opt_info.get("exp")
            dte_remaining = (exp - today).days if exp else 0
            spot = get_spot(data, sym)
            if spot and (dte_remaining > MIN_DTE_TO_ROLL
                         and should_roll_put(spot, put_strike, ROLL_TRIGGER_PUT)
                         and ledger.get(sym).rolls < MAX_ROLLS):
                lo, hi = put_strike_band(spot, cfg.put.band)
                cands = fetch_contracts(opt, sym, "put", lo, hi, cfg.put.dte, today)
                pick = select_put(cands, spot, cfg.put, today, equity,
                                  cfg.per_stock_cap_pct, exposure, cfg.portfolio_wheel_cap_pct)
                if pick:
                    new_c, y = pick
                    rolled = _execute_roll(trading, opt, occ, new_c,
                                           cfg.safeguards.limit_slippage_pct,
                                           dry_run, summary, ledger.get(sym), qty=put_qty,
                                           account=cfg.account)
                    if rolled:
                        # Adjust exposure: swap old put's capital requirement for new one.
                        exposure += (new_c.strike - put_strike) * 100 * put_qty
                else:
                    summary["skipped"].append(f"{sym}: PUT near ITM but no roll candidate")
            elif not spot:
                summary["skipped"].append(f"{sym}: no quote for roll check")

        elif state == WheelState.CALL_OPEN:
            opt_info = pv.options.get(sym, {})
            occ = opt_info.get("occ", "")
            call_strike = opt_info.get("strike", 0.0)
            call_qty = opt_info.get("qty", 1)
            exp = opt_info.get("exp")
            dte_remaining = (exp - today).days if exp else 0
            spot = get_spot(data, sym)
            if spot and (dte_remaining > MIN_DTE_TO_ROLL
                         and should_roll_call(spot, call_strike, ROLL_TRIGGER_CALL)
                         and ledger.get(sym).rolls < MAX_ROLLS):
                shr = pv.shares.get(sym, {})
                basis = ledger.get(sym).cost_basis or shr.get("basis", spot)
                cands = fetch_contracts(
                    opt, sym, "call",
                    *call_strike_band(max(basis, spot), cfg.call.band),
                    cfg.call.dte, today)
                pick = select_call(cands, basis, spot, cfg.call, today)
                if pick:
                    new_c, y = pick
                    _execute_roll(trading, opt, occ, new_c,
                                  cfg.safeguards.limit_slippage_pct,
                                  dry_run, summary, ledger.get(sym), qty=call_qty,
                                  account=cfg.account)
                else:
                    summary["skipped"].append(f"{sym}: CALL near ITM but no roll candidate")
            elif not spot:
                summary["skipped"].append(f"{sym}: no quote for roll check")

        led = ledger.get(sym)
        led.last_state = state.value  # persist for transition detection next cycle
        if state in (WheelState.PUT_OPEN, WheelState.CALL_OPEN):
            cur_exp = pv.options.get(sym, {}).get("exp")
            led.last_exp = cur_exp.isoformat() if cur_exp else ""
        else:
            led.last_exp = ""
        summary["metrics"][sym] = {
            "state": state.value,
            "premium_collected": round(led.premium_collected, 2),
            "cost_basis": round(led.cost_basis, 2),
            "realized_pnl": round(led.realized_pnl, 2),
            "rolls": led.rolls,
        }

    if not dry_run:
        ledger.save()
    summary["exposure_pct"] = round(exposure / equity * 100, 1) if equity else 0
    return summary


def _alert(text: str, account: str = "default") -> bool:
    """Never raises. send_telegram already retries transient failures internally;
    if it still returns False, log it clearly so a dropped alert shows up in the
    run's logs instead of vanishing. Non-default accounts get a tag prefix so
    multi-account alerts are distinguishable.

    Returns whether the send actually succeeded, so callers that track "already
    notified today" state can skip marking it done on a failed send -- otherwise
    a single dropped message is lost forever instead of retried next cycle.
    """
    if account != "default":
        text = f"[{account.upper()}] {text}"
    try:
        from bot.notify import send_telegram  # lazy import — keep engine testable without Telegram env
        ok = send_telegram(text)
        if not ok:
            log.error("Telegram alert dropped after retries: %.80s...", text)
        return ok
    except Exception as exc:  # noqa: BLE001
        log.warning("alert send failed: %s", exc)
        return False


def _notify_transition(sym: str, old: str, new: str, led: SymbolLedger,
                       account: str = "default") -> None:
    """Send a Telegram alert when a symbol's wheel state changes meaningfully."""
    if old == "PUT_OPEN" and new == "LONG_STOCK":
        basis = f"${led.cost_basis:.2f}" if led.cost_basis else "at strike"
        _alert(
            f"📦 *{sym} — assigned*\n"
            f"Now holding 100 shares · cost basis {basis}\n"
            f"Premium banked so far: +${led.premium_collected:.2f}\n"
            f"Will sell a covered call next cycle",
            account,
        )
    elif old == "PUT_OPEN" and new == "CASH":
        _alert(
            f"✅ *{sym} — put expired worthless*\n"
            f"Full premium kept: +${led.premium_collected:.2f}\n"
            f"Back to CASH · will sell a new put",
            account,
        )
    elif old == "CALL_OPEN" and new == "CASH":
        _alert(
            f"🎯 *{sym} — called away · wheel cycle complete!*\n"
            f"Realized P&L: ${led.realized_pnl:+.2f}",
            account,
        )
    elif old == "CALL_OPEN" and new == "LONG_STOCK":
        _alert(
            f"📋 *{sym} — covered call expired*\n"
            f"Still holding 100 shares · will sell a new call",
            account,
        )


def _send_morning_briefing(pv: "PositionsView", equity: float,
                           exposure: float, cfg: "WheelConfig") -> bool:
    open_puts  = [f"• {sym} PUT ${info['strike']:.2f} exp {info['exp']}"
                  for sym, info in pv.options.items() if info["type"] == "put"]
    open_calls = [f"• {sym} CALL ${info['strike']:.2f} exp {info['exp']}"
                  for sym, info in pv.options.items() if info["type"] == "call"]
    shares_    = [f"• {sym} ×{int(info['qty'])} @ ${info['price']:.2f}"
                  for sym, info in pv.shares.items()]
    watching   = [s for s in cfg.universe if s not in pv.wheel_names()]

    lines = [f"🌅 *Wheel — market open*",
             f"Equity: ${equity:,.2f}  ·  Deployed: {exposure/equity*100:.1f}%"]
    if open_puts:
        lines += ["", "*Short puts:*"] + open_puts
    if open_calls:
        lines += ["", "*Covered calls:*"] + open_calls
    if shares_:
        lines += ["", "*Shares held:*"] + shares_
    if watching:
        lines += ["", f"Watching for new puts: {', '.join(watching)}"]
    return _alert("\n".join(lines), cfg.account)


def _near_market_close(now: datetime) -> bool:
    """True during the 20:00-21:59 UTC window, which covers 4pm ET whether EDT
    (20:00 UTC) or EST (21:00 UTC) -- with margin either side.

    The EOD summary is only attempted in this window (never on a pre-market
    tick) so that if every post-close cycle that day gets skipped for some
    reason, the bot doesn't fire a "market closed" message the *next* morning
    mislabeled as if it just happened -- it simply skips that day's summary,
    which the wheel-monitor watchdog would separately flag as a dispatch gap.
    """
    return now.hour >= 20


def _send_eod_summary(pv: "PositionsView", equity: float,
                      total_premium: float, total_pnl: float,
                      account: str = "default") -> bool:
    open_count = len(pv.options) + len(pv.shares)
    lines = [
        f"🔔 *Wheel — market closed*",
        f"Equity: ${equity:,.2f}",
        f"Premium collected (all time): +${total_premium:.2f}",
        f"Net realized P&L: ${total_pnl:+.2f}",
        f"Open positions: {open_count}",
    ]
    if pv.options:
        lines.append("")
        for sym, info in pv.options.items():
            lines.append(f"• {sym} {info['type'].upper()} ${info['strike']:.2f} exp {info['exp']}")
    return _alert("\n".join(lines), account)


def _wait_for_fill(trading: TradingClient, order_id: str,
                   timeout_s: int = 45, poll_s: int = 5) -> bool:
    """Poll an order until it fills or reaches a terminal non-fill status.

    We wait before placing the STO leg of a roll so that if the BTC doesn't fill
    (e.g. limit too tight, illiquid), the STO is never submitted and the position
    remains intact. This prevents the partial-roll scenario where BTC fills but STO
    is never placed, leaving the account without coverage until the next cycle.
    """
    _FILLED = {"filled", "partially_filled"}
    _TERMINAL = {"cancelled", "expired", "rejected", "done_for_day"}
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            o = trading.get_order_by_id(order_id)
            status = getattr(o.status, "value", str(o.status))
            if status in _FILLED:
                return True
            if status in _TERMINAL:
                log.warning("order %s ended with status %s", order_id, status)
                return False
        except Exception as exc:  # noqa: BLE001
            log.warning("poll order %s failed: %s", order_id, exc)
        time.sleep(poll_s)
    log.warning("order %s did not fill within %ds", order_id, timeout_s)
    return False


def _current_ask(opt: OptionHistoricalDataClient, occ: str) -> float | None:
    """Fetch the live ask price for a specific OCC option symbol."""
    try:
        under, exp, typ, strike = parse_occ(occ)
    except ValueError:
        return None
    req = OptionChainRequest(
        underlying_symbol=under,
        feed="indicative",
        type=ContractType.PUT if typ == "put" else ContractType.CALL,
        strike_price_gte=strike - 0.01,
        strike_price_lte=strike + 0.01,
        expiration_date_gte=exp.isoformat(),
        expiration_date_lte=exp.isoformat(),
    )
    try:
        chain = opt.get_option_chain(req)
    except Exception as exc:  # noqa: BLE001
        log.warning("ask fetch failed for %s: %s", occ, exc)
        return None
    snap = (chain or {}).get(occ)
    if not snap:
        return None
    quote = getattr(snap, "latest_quote", None)
    val = float(getattr(quote, "ask_price", 0) or 0) if quote else 0.0
    return val or None


def _execute_roll(trading: TradingClient, opt: OptionHistoricalDataClient,
                  occ: str, new_c: Contract, slippage_pct: float,
                  dry_run: bool, summary: dict, led: SymbolLedger, qty: int = 1,
                  account: str = "default") -> bool:
    """Buy-to-close the existing option and sell-to-open the new one for a net credit.

    qty must match the existing position's contract count — closing fewer would
    leave a partial short open, closing more would fail outright.
    Returns True only if both live orders were submitted successfully.
    """
    existing_ask = _current_ask(opt, occ)
    if not existing_ask:
        log.warning("ROLL: cannot get ask for %s — skipping", occ)
        return False
    net_credit = new_c.bid - existing_ask
    if net_credit <= 0:
        log.info("ROLL: no net credit for %s -> %s (%.2f - %.2f = %.2f) — skipping",
                 occ, new_c.symbol, new_c.bid, existing_ask, net_credit)
        return False
    btc_limit = round(existing_ask * (1 + slippage_pct), 2)   # pay slightly over ask to fill
    sto_limit = round(new_c.bid * (1 - slippage_pct), 2)      # receive slightly under bid
    msg = (f"ROLL {occ} -> {new_c.symbol} x{qty}: "
           f"BTC limit ${btc_limit:.2f} | STO limit ${sto_limit:.2f} "
           f"| net ${net_credit * 100 * qty:.2f}")
    if sto_limit <= 0:
        log.warning("ROLL: STO limit <= 0 for %s — skipping", new_c.symbol)
        return False
    if dry_run:
        log.info("[dry-run] %s", msg)
        summary["actions"].append(f"[dry-run] {msg}")
        return False
    try:
        btc = trading.submit_order(LimitOrderRequest(
            symbol=occ, qty=qty, side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY, limit_price=btc_limit))
        log.info("BTC %s x%d -> order %s (awaiting fill)", occ, qty, btc.id)
    except Exception as exc:  # noqa: BLE001
        log.error("BTC order failed for %s: %s", occ, exc)
        return False
    # Wait for the BTC to fill before placing STO. If BTC doesn't fill (e.g. stale
    # ask, illiquid market), the STO is never submitted — the position stays open
    # and rolls is not incremented. The next cycle will retry.
    if not _wait_for_fill(trading, str(btc.id)):
        log.warning("ROLL: BTC %s did not fill — STO skipped, will retry next cycle", occ)
        _alert(f"⚠️ *Wheel roll* — BTC `{occ}` did not fill within 45s. "
               f"STO skipped; bot will retry next cycle.", account)
        return False
    try:
        sto = trading.submit_order(LimitOrderRequest(
            symbol=new_c.symbol, qty=qty, side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY, limit_price=sto_limit))
        log.info("STO %s x%d -> order %s", new_c.symbol, qty, sto.id)
    except Exception as exc:  # noqa: BLE001
        log.error("STO failed for %s after BTC filled: %s", new_c.symbol, exc)
        _alert(f"⚠️ *Wheel roll* — BTC `{occ}` filled but STO `{new_c.symbol}` failed. "
               f"Position closed; next cycle opens a fresh put.", account)
        return False
    summary["actions"].append(msg)
    led.rolls += 1                             # only incremented after both legs confirmed
    led.premium_collected += net_credit * 100 * qty
    return True


def _sell_to_open(trading, c: Contract, yld: float, cfg: WheelConfig, dry_run, summary, qty: int = 1):
    limit = round(c.bid * (1 - cfg.safeguards.limit_slippage_pct), 2)
    msg = (f"SELL-TO-OPEN {c.type.upper()} {c.symbol} x{qty} "
           f"strike ${c.strike:.2f} bid ${c.bid:.2f} -> limit ${limit:.2f} "
           f"(~{yld*100:.1f}% ann.)")
    if dry_run or limit <= 0:
        log.info("[dry-run] %s", msg)
        summary["actions"].append(f"[dry-run] {msg}")
        return
    order = trading.submit_order(LimitOrderRequest(
        symbol=c.symbol, qty=qty, side=OrderSide.SELL,
        time_in_force=TimeInForce.DAY, limit_price=limit))
    log.info("%s -> order %s", msg, order.id)
    summary["actions"].append(msg)
