"""Run one momentum-strategy cycle. DRY-RUN BY DEFAULT.

    python momentum_run.py              # dry-run: logs the intended rotation, trades nothing
    python momentum_run.py --live       # actually place orders (requires enabled:true
                                        #   in config.momentum.yaml)

No-ops on every call except the first one in a new calendar month (state
tracked in docs/momentum_state_<account>.json) and outside market hours.
Meant to be triggered once a day, not every 5 minutes -- see
.github/workflows/momentum.yml.
"""

from __future__ import annotations

import argparse
import logging
import sys

from bot.momentum.config import load_momentum_config
from bot.momentum.engine import run_momentum_cycle


def main() -> int:
    ap = argparse.ArgumentParser(description="Run one momentum-strategy cycle.")
    ap.add_argument("--live", action="store_true",
                    help="Place real (paper) orders. Default is dry-run.")
    ap.add_argument("--config", default=None,
                    help="Path to a momentum config YAML (default: config.momentum.yaml).")
    ap.add_argument("--account", default="momentum",
                    help="Account slug (default: 'momentum'). Reads "
                         "ALPACA_<SLUG>_API_KEY/SECRET and writes docs/momentum_state_<slug>.json.")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    cfg = load_momentum_config(args.config, args.account)
    dry = not args.live
    logging.getLogger("momentum").info("=== Momentum cycle [%s] account=%s ===",
                                       "DRY-RUN" if dry else "LIVE (paper)", cfg.account)

    try:
        summary = run_momentum_cycle(cfg, dry_run=dry)
    except Exception:
        logging.getLogger("momentum").exception("Momentum cycle failed")
        return 1

    print("\n--- Momentum summary ---")
    print(f"Holding: {summary.get('holding') or 'CASH'}")
    print(f"Rebalanced this run: {summary.get('rebalanced')}")
    for a in summary.get("actions", []):
        print(f"  - {a}")
    if summary.get("skipped"):
        print(f"Skipped: {summary['skipped']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
