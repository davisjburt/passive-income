"""One-shot script: cancel all orders, close all positions, wipe the local ledger.

    python reset_paper.py                          # preview the default (conservative) account
    python reset_paper.py --live                    # actually reset the default account
    python reset_paper.py --account aggressive       # preview the aggressive account
    python reset_paper.py --account aggressive --live  # actually reset the aggressive account
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv
from bot.wheel.accounts import get_account
from bot.wheel.config import load_wheel_config
from bot.wheel.engine import ledger_path as wheel_ledger_path

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="Actually cancel/close. Default is dry-run.")
    ap.add_argument("--account", default="default",
                    help="Account slug (default: 'default'), must be registered in "
                         "bot/wheel/accounts.py. Matches wheel_run.py's --account -- "
                         "'aggressive' resets the $100k aggressive account instead of the "
                         "conservative one.")
    ap.add_argument("--config", default=None,
                    help="Path to a wheel config YAML. Defaults to whatever "
                         "bot/wheel/accounts.py registers for --account.")
    args = ap.parse_args()
    dry = not args.live

    cfg_path = args.config
    if cfg_path is None:
        try:
            cfg_path = get_account(args.account).config
        except KeyError as exc:
            raise SystemExit(f"{exc} (or pass --config explicitly for a one-off account)")
    cfg = load_wheel_config(cfg_path, args.account)
    from alpaca.trading.client import TradingClient
    t = TradingClient(cfg.api_key, cfg.api_secret, paper=True)

    print(f"\nAccount        : {args.account}")
    acct = t.get_account()
    print(f"Account equity : ${float(acct.equity):,.2f}")
    print(f"Buying power   : ${float(acct.buying_power):,.2f}")

    # Open orders
    orders = t.get_orders()
    print(f"\nOpen orders    : {len(orders)}")
    for o in orders:
        print(f"  {o.symbol}  {getattr(o.side,'value',o.side)}  qty={o.qty}  limit={o.limit_price}")

    # Open positions
    positions = t.get_all_positions()
    print(f"\nOpen positions : {len(positions)}")
    for p in positions:
        print(f"  {p.symbol}  qty={p.qty}  market_value=${float(p.market_value or 0):,.2f}")

    if dry:
        print("\n[dry-run] Nothing changed. Re-run with --live to execute.")
        return 0

    confirm = input("\nType YES to cancel all orders and close all positions: ")
    if confirm.strip() != "YES":
        print("Aborted.")
        return 1

    print("\nCancelling all orders…")
    t.cancel_orders()
    print("  done.")

    print("Closing all positions…")
    t.close_all_positions(cancel_orders=True)
    print("  done.")

    # Wipe local ledger so the bot starts fresh
    ledger_path = wheel_ledger_path(args.account)
    if ledger_path.exists():
        ledger_path.write_text("{}")
        print(f"Cleared {ledger_path.name}.")

    print("\nDone. Account is flat. Run the bot to start fresh.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
