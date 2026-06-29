"""Friday end-of-week job (run in CI, where Alpaca credentials live).

Steps:
  1. Cancel any outstanding orders (end the week with a clean order book).
  2. Generate the weekly trade report (docs/reports/<date>.md + index.json).
  3. Refresh the dashboard snapshot (docs/data.json).

The Claude review routine reads the committed report afterwards and decides
whether to adjust the model.

Usage:
    python weekly.py            # live (paper)
    python weekly.py --dry-run  # don't cancel orders; still generate report
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime

from bot.client import make_trading_client
from bot.config import load_config
from bot.maintenance import cancel_open_orders
from bot.notify import send_telegram
from bot.report import write_report
from bot.weekly_report import build_report, write_weekly_report


def main() -> int:
    parser = argparse.ArgumentParser(description="Weekly end-of-week job.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Do not cancel orders; still generate reports.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    log = logging.getLogger("bot")
    cfg = load_config()

    log.info("=== Weekly job start ===")
    try:
        trading = make_trading_client(cfg)
        cancelled = cancel_open_orders(trading, dry_run=args.dry_run)
        if cancelled:
            log.info("Outstanding orders cancelled: %s", ", ".join(cancelled))

        report_path = write_weekly_report(cfg)
        write_report(cfg)  # keep the dashboard snapshot fresh too
    except Exception:
        log.exception("Weekly job failed")
        return 1

    if not args.dry_run:
        try:
            s = build_report(cfg)["summary"]
            sign = "🟢" if s["realized_pl"] >= 0 else "🔴"
            send_telegram(
                f"📊 *Weekly report* — week ending {datetime.now().strftime('%Y-%m-%d')}\n"
                f"{sign} Realized P&L: ${s['realized_pl']:,.2f}\n"
                f"Return: {s['week_return_pct']:+.2f}% · "
                f"{s['num_trades']} trades · {s['win_rate']:.0f}% win rate"
            )
        except Exception:
            log.exception("Weekly notification failed")

    print(f"\nWeekly report written: {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
