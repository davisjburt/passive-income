"""Run one options-wheel cycle. DRY-RUN BY DEFAULT.

    python wheel_run.py              # dry-run: logs intended trades, places nothing
    python wheel_run.py --live       # actually place orders (requires options Level 1
                                     #   enabled AND enabled:true in config.wheel.yaml)

This is separate from the RSI bot (run.py). It is intentionally NOT wired into any
GitHub Actions workflow yet — add one only after you've validated dry-runs.
"""

from __future__ import annotations

import argparse
import logging
import sys

from bot.wheel.config import load_wheel_config
from bot.wheel.engine import run_wheel_cycle


def main() -> int:
    ap = argparse.ArgumentParser(description="Run one options-wheel cycle.")
    ap.add_argument("--live", action="store_true",
                    help="Place real (paper) orders. Default is dry-run.")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    cfg = load_wheel_config()
    dry = not args.live
    logging.getLogger("wheel").info("=== Wheel cycle [%s] ===", "DRY-RUN" if dry else "LIVE (paper)")

    try:
        summary = run_wheel_cycle(cfg, dry_run=dry)
    except Exception:
        logging.getLogger("wheel").exception("Wheel cycle failed")
        return 1

    print("\n--- Wheel summary ---")
    print(f"Exposure: {summary.get('exposure_pct', 0)}% of equity")
    for a in summary.get("actions", []) or ["(no orders)"]:
        print(f"  - {a}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
