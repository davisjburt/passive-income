"""Weekly trade report: every closed trade with realized P&L, plus a summary.

Writes a Markdown file to docs/reports/ and updates docs/reports/index.json so the
dashboard can link to it. Realized P&L is computed by FIFO-matching fills.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest, GetPortfolioHistoryRequest

from .client import make_trading_client
from .config import Config

log = logging.getLogger("bot")

REPORTS = Path(__file__).resolve().parent.parent / "docs" / "reports"
EPS = 1e-9


def match_trades(fills: list[dict]) -> tuple[list[dict], dict]:
    """FIFO-match buy/sell fills into closed round-trip trades.

    `fills`: list of {symbol, side('buy'|'sell'), qty, price, time}, sorted oldest first.
    Returns (closed_trades, open_lots).
    """
    lots: dict[str, deque] = defaultdict(deque)
    closed: list[dict] = []
    for f in fills:
        sym, qty, price, t = f["symbol"], f["qty"], f["price"], f["time"]
        if qty <= EPS:
            continue
        if f["side"] == "buy":
            lots[sym].append([qty, price, t])
            continue
        # sell: consume oldest lots first
        remaining = qty
        while remaining > EPS and lots[sym]:
            lot = lots[sym][0]
            take = min(remaining, lot[0])
            pnl = (price - lot[1]) * take
            closed.append({
                "symbol": sym,
                "qty": take,
                "entry_time": lot[2],
                "entry_price": lot[1],
                "exit_time": t,
                "exit_price": price,
                "pnl": pnl,
                "pnl_pct": (price / lot[1] - 1) * 100 if lot[1] else 0.0,
            })
            lot[0] -= take
            remaining -= take
            if lot[0] <= EPS:
                lots[sym].popleft()
        # remaining unmatched (e.g. a short) is ignored — this bot is long-only
    open_lots = {s: [list(x) for x in dq] for s, dq in lots.items() if dq}
    return closed, open_lots


def _fills_from_orders(trading) -> list[dict]:
    orders = trading.get_orders(
        GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=500, direction="asc")
    )
    fills = []
    for o in orders:
        if not o.filled_at or not o.filled_avg_price or not o.filled_qty:
            continue
        if float(o.filled_qty) <= 0:
            continue
        side = o.side.value if hasattr(o.side, "value") else str(o.side)
        fills.append({
            "symbol": o.symbol,
            "side": side,
            "qty": float(o.filled_qty),
            "price": float(o.filled_avg_price),
            "time": o.filled_at,
        })
    fills.sort(key=lambda f: f["time"])
    return fills


def _weekly_equity(trading) -> tuple[float, float]:
    """(start_equity, end_equity) over roughly the last trading week."""
    try:
        ph = trading.get_portfolio_history(
            GetPortfolioHistoryRequest(period="1M", timeframe="1D")
        )
        eq = [float(e) for e in (ph.equity or []) if e and float(e) > 0]
        if len(eq) >= 2:
            week = eq[-6:]  # ~5 trading days + today
            return week[0], week[-1]
        if eq:
            return eq[-1], eq[-1]
    except Exception as exc:  # noqa: BLE001
        log.warning("weekly equity unavailable: %s", exc)
    return 0.0, 0.0


def build_report(cfg: Config, days: int = 7) -> dict:
    trading = make_trading_client(cfg)
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=days)

    fills = _fills_from_orders(trading)
    closed, _open_lots = match_trades(fills)

    # Trades whose EXIT happened in the report window.
    week_trades = [t for t in closed if t["exit_time"] >= window_start]
    week_trades.sort(key=lambda t: t["exit_time"])

    realized = sum(t["pnl"] for t in week_trades)
    wins = [t for t in week_trades if t["pnl"] > 0]
    losses = [t for t in week_trades if t["pnl"] < 0]
    win_rate = (len(wins) / len(week_trades) * 100) if week_trades else 0.0
    avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0.0
    avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0.0

    positions = []
    for p in trading.get_all_positions():
        positions.append({
            "symbol": p.symbol,
            "qty": float(p.qty),
            "avg_entry": float(p.avg_entry_price),
            "current_price": float(p.current_price),
            "unrealized_pl": float(p.unrealized_pl),
            "unrealized_plpc": float(p.unrealized_plpc) * 100,
        })

    eq_start, eq_end = _weekly_equity(trading)
    week_return = ((eq_end - eq_start) / eq_start * 100) if eq_start else 0.0

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "week_ending": now.strftime("%Y-%m-%d"),
        "window_days": days,
        "trades": week_trades,
        "summary": {
            "num_trades": len(week_trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(win_rate, 1),
            "realized_pl": round(realized, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "equity_start": round(eq_start, 2),
            "equity_end": round(eq_end, 2),
            "week_return_pct": round(week_return, 2),
        },
        "open_positions": positions,
    }


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d") if dt else "—"


def _money(n: float) -> str:
    return f"{'-' if n < 0 else ''}${abs(n):,.2f}"


def render_markdown(r: dict) -> str:
    s = r["summary"]
    lines = [
        f"# Weekly trading report — week ending {r['week_ending']}",
        "",
        f"_Generated {r['generated_at']} · window: last {r['window_days']} days · paper account_",
        "",
        "## Summary",
        "",
        f"- **Realized P&L:** {_money(s['realized_pl'])}",
        f"- **Equity:** {_money(s['equity_start'])} → {_money(s['equity_end'])} "
        f"(**{s['week_return_pct']:+.2f}%** for the week)",
        f"- **Closed trades:** {s['num_trades']} · "
        f"**Win rate:** {s['win_rate']:.0f}% ({s['wins']}W / {s['losses']}L)",
        f"- **Avg win:** {_money(s['avg_win'])} · **Avg loss:** {_money(s['avg_loss'])}",
        "",
        "## Closed trades",
        "",
    ]
    if r["trades"]:
        lines.append("| Symbol | Qty | Entry | Exit | Entry $ | Exit $ | Hold | P&L | P&L % |")
        lines.append("|---|---:|---|---|---:|---:|---:|---:|---:|")
        for t in r["trades"]:
            hold = (t["exit_time"] - t["entry_time"]).days
            lines.append(
                f"| {t['symbol']} | {t['qty']:.4f} | {_fmt(t['entry_time'])} | "
                f"{_fmt(t['exit_time'])} | ${t['entry_price']:,.2f} | ${t['exit_price']:,.2f} | "
                f"{hold}d | {_money(t['pnl'])} | {t['pnl_pct']:+.2f}% |"
            )
    else:
        lines.append("_No trades closed this week._")

    lines += ["", "## Open positions (week end)", ""]
    if r["open_positions"]:
        lines.append("| Symbol | Qty | Avg entry | Last | Unrealized P&L | % |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for p in r["open_positions"]:
            lines.append(
                f"| {p['symbol']} | {p['qty']:.4f} | ${p['avg_entry']:,.2f} | "
                f"${p['current_price']:,.2f} | {_money(p['unrealized_pl'])} | "
                f"{p['unrealized_plpc']:+.2f}% |"
            )
    else:
        lines.append("_No open positions — fully in cash._")

    lines.append("")
    return "\n".join(lines)


def write_weekly_report(cfg: Config) -> Path:
    REPORTS.mkdir(parents=True, exist_ok=True)
    r = build_report(cfg)
    md_path = REPORTS / f"{r['week_ending']}.md"
    md_path.write_text(render_markdown(r))

    # Maintain an index of all reports (newest first) for the dashboard.
    index_path = REPORTS / "index.json"
    existing = []
    if index_path.exists():
        try:
            existing = json.loads(index_path.read_text())
        except json.JSONDecodeError:
            existing = []
    entry = {
        "week_ending": r["week_ending"],
        "file": f"{r['week_ending']}.md",
        "realized_pl": r["summary"]["realized_pl"],
        "week_return_pct": r["summary"]["week_return_pct"],
        "num_trades": r["summary"]["num_trades"],
    }
    index = [e for e in existing if e.get("week_ending") != r["week_ending"]]
    index.insert(0, entry)
    index_path.write_text(json.dumps(index, indent=2))

    log.info("Wrote weekly report %s (realized P&L %s, %d trades)",
             md_path, _money(r["summary"]["realized_pl"]), r["summary"]["num_trades"])
    return md_path
