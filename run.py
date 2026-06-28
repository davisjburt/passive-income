"""Entry point. Runs one trading cycle and prints a summary.

Usage:
    python run.py            # live (paper) cycle
    python run.py --dry-run  # compute signals & intended orders, submit nothing
"""

from __future__ import annotations

import argparse
import logging
import sys

from bot.config import load_config
from bot.trader import run_cycle


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one trading cycle.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Evaluate signals and log intended orders without submitting them.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    cfg = load_config()
    mode = "DRY-RUN" if args.dry_run else "LIVE (paper)"
    logging.getLogger("bot").info("=== Trading cycle start [%s] ===", mode)

    try:
        summary = run_cycle(cfg, dry_run=args.dry_run)
    except Exception:
        logging.getLogger("bot").exception("Cycle failed")
        return 1

    actions = summary.get("actions", [])
    print("\n--- Summary ---")
    print(f"Market open : {summary.get('market_open')}")
    print(f"Loss halt   : {summary.get('halted')}")
    if actions:
        print("Actions:")
        for a in actions:
            print(f"  - {a}")
    else:
        print("Actions     : none (no signals / no slots)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
