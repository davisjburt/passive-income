"""Single owner of the "market open" / "market closed" Telegram recap.

Market hours are the NYSE calendar -- identical for every Alpaca account, paper
or otherwise. Previously each account's own wheel_run.py cycle independently
checked the clock and sent its own morning-briefing / EOD-summary message: N
accounts meant N clock checks and N near-simultaneous Telegram pings for what
is actually a single, shared fact. This script is the one place that decides
"is the market open right now" and sends exactly one combined message covering
every account in bot.wheel.accounts.ACCOUNTS.

Runs on its own schedule (.github/workflows/recap.yml), completely decoupled
from the per-account trading workflows (wheel.yml, wheel-aggressive.yml, ...).
Those keep doing their own trading and their own per-trade alerts (assignment,
roll, drawdown-halt) -- those are genuinely account-specific events, not
duplicated by this script. This script owns its own state file
(docs/recap_state.json), so it never touches the files the trading workflows
commit and there's nothing for the workflows to race or fight over. Adding a
new account is a one-line addition to bot/wheel/accounts.py (plus its own repo
secrets in recap.yml's env block, and its own trading workflow) -- no other
workflow needs to change.

    python scripts/send_recap.py
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root, for `bot.*`

from alpaca.trading.client import TradingClient

from bot.notify import send_telegram
from bot.wheel.accounts import ACCOUNTS, AccountInfo
from bot.wheel.config import load_wheel_config
from bot.wheel.engine import _with_retries, build_positions_view
from bot.wheel.report import build_wheel_report

log = logging.getLogger("recap")
STATE_PATH = Path(__file__).resolve().parents[1] / "docs" / "recap_state.json"


def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def _near_market_close(now: datetime) -> bool:
    """True during the 20:00-21:59 UTC window, which covers 4pm ET whether EDT
    (20:00 UTC) or EST (21:00 UTC) -- with margin either side.

    The close recap is only attempted in this window (never on a pre-market
    tick) so that if every post-close cycle that day gets skipped for some
    reason, this doesn't fire a "market closed" message the *next* morning
    mislabeled as if it just happened -- it simply skips that day's recap,
    which the wheel-monitor watchdog would separately flag as a dispatch gap.
    """
    return now.hour >= 20


def _open_section(info: AccountInfo) -> str:
    cfg = load_wheel_config(info.config, info.slug)
    t = TradingClient(cfg.api_key, cfg.api_secret, paper=cfg.paper)
    equity = float(_with_retries(t.get_account, what=f"get_account[{info.slug}]").equity)
    pv = build_positions_view(t)
    exposure = pv.exposure()
    lines = [f"*{info.label}*",
             f"Equity: ${equity:,.2f}" + (f" · Deployed: {exposure/equity*100:.1f}%" if equity else "")]
    open_puts = [f"• {sym} PUT ${p['strike']:.2f} exp {p['exp']}"
                 for sym, p in pv.options.items() if p["type"] == "put"]
    open_calls = [f"• {sym} CALL ${p['strike']:.2f} exp {p['exp']}"
                  for sym, p in pv.options.items() if p["type"] == "call"]
    shares_ = [f"• {sym} ×{int(p['qty'])} @ ${p['price']:.2f}" for sym, p in pv.shares.items()]
    watching = [s for s in cfg.universe if s not in pv.wheel_names()]
    lines += open_puts + open_calls + shares_
    if watching:
        lines.append(f"Watching: {', '.join(watching)}")
    return "\n".join(lines)


def _close_section(info: AccountInfo) -> str:
    cfg = load_wheel_config(info.config, info.slug)
    t = TradingClient(cfg.api_key, cfg.api_secret, paper=cfg.paper)
    equity = float(_with_retries(t.get_account, what=f"get_account[{info.slug}]").equity)
    report = build_wheel_report(cfg)
    return "\n".join([
        f"*{info.label}*",
        f"Equity: ${equity:,.2f}",
        f"Premium collected (all time): +${report['total_premium_collected']:.2f}",
        f"Net realized P&L: ${report['total_realized_pnl']:+.2f}",
        f"Open positions: {len(report['option_positions'])}",
    ])


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    # Market hours are account-agnostic -- one clock check via the first
    # registered account's credentials speaks for every account.
    anchor_cfg = load_wheel_config(ACCOUNTS[0].config, ACCOUNTS[0].slug)
    anchor = TradingClient(anchor_cfg.api_key, anchor_cfg.api_secret, paper=anchor_cfg.paper)
    clock = _with_retries(anchor.get_clock, what="get_clock")

    state = _load_state()
    now = datetime.now(timezone.utc)
    today_str = now.date().isoformat()

    if clock.is_open:
        if state.get("morning_date") == today_str:
            log.info("Morning recap already sent today.")
            return 0
        sections = [_open_section(a) for a in ACCOUNTS]
        text = "🌅 *Wheel — market open*\n\n" + "\n\n".join(sections)
        if send_telegram(text):
            state["morning_date"] = today_str
            _save_state(state)
        else:
            log.error("Morning recap send failed; will retry next cycle.")
        return 0

    if not _near_market_close(now):
        log.info("Market closed, outside the evening recap window -- nothing to do.")
        return 0

    if state.get("eod_date") == today_str:
        log.info("EOD recap already sent today.")
        return 0
    sections = [_close_section(a) for a in ACCOUNTS]
    text = "🔔 *Wheel — market closed*\n\n" + "\n\n".join(sections)
    if send_telegram(text):
        state["eod_date"] = today_str
        _save_state(state)
    else:
        log.error("EOD recap send failed; will retry next cycle.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
