"""Run one options-wheel cycle. DRY-RUN BY DEFAULT.

    python wheel_run.py              # dry-run: logs intended trades, places nothing
    python wheel_run.py --live       # actually place orders (requires options Level 1
                                     #   enabled AND enabled:true in config.wheel.yaml)

Triggered every 5 min via Cloudflare Worker → wheel.yml workflow_dispatch.
"""

from __future__ import annotations

import argparse
import logging
import sys

from bot.wheel.accounts import get_account
from bot.wheel.config import load_wheel_config
from bot.wheel.engine import run_wheel_cycle


def main() -> int:
    ap = argparse.ArgumentParser(description="Run one options-wheel cycle.")
    ap.add_argument("--live", action="store_true",
                    help="Place real (paper) orders. Default is dry-run.")
    ap.add_argument("--config", default=None,
                    help="Path to a wheel config YAML. Defaults to whatever "
                         "bot/wheel/accounts.py registers for --account.")
    ap.add_argument("--account", default="default",
                    help="Account slug (default: 'default'), must be registered in "
                         "bot/wheel/accounts.py. Non-default slugs read "
                         "ALPACA_<SLUG>_API_KEY/SECRET instead of ALPACA_API_KEY/SECRET, "
                         "write to separate wheel_<slug>.json / wheel_ledger_<slug>.json "
                         "files, and tag Telegram alerts with [<SLUG>].")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    cfg_path = args.config
    if cfg_path is None:
        try:
            cfg_path = get_account(args.account).config
        except KeyError as exc:
            raise SystemExit(f"{exc} (or pass --config explicitly for a one-off account)")
    cfg = load_wheel_config(cfg_path, args.account)
    dry = not args.live
    logging.getLogger("wheel").info("=== Wheel cycle [%s] account=%s ===",
                                    "DRY-RUN" if dry else "LIVE (paper)", cfg.account)

    try:
        summary = run_wheel_cycle(cfg, dry_run=dry)
    except Exception:
        logging.getLogger("wheel").exception("Wheel cycle failed")
        return 1

    # Always refresh the dashboard's wheel data (positions + pending orders).
    try:
        from bot.wheel.report import write_wheel_report
        write_wheel_report(cfg)
    except Exception:
        logging.getLogger("wheel").exception("Wheel report failed")

    print("\n--- Wheel summary ---")
    print(f"Exposure: {summary.get('exposure_pct', 0)}% of equity")
    metrics = summary.get("metrics", {})
    if metrics:
        rolled = {s: m["rolls"] for s, m in metrics.items() if m.get("rolls")}
        if rolled:
            print("Rolls this position: " + ", ".join(f"{s}×{n}" for s, n in rolled.items()))
    print("Orders:")
    for a in summary.get("actions", []) or ["(none)"]:
        print(f"  - {a}")
    skipped = summary.get("skipped", [])
    if skipped:
        print("Skipped (why nothing fired):")
        for sline in skipped:
            print(f"  - {sline}")

    # Notify on real orders only (not dry-runs).
    actions = summary.get("actions", [])
    if not dry and actions:
        from bot.notify import send_telegram
        equity = summary.get("equity", 0)
        exposure = summary.get("exposure_pct", 0)
        tag = f"[{cfg.account.upper()}] " if cfg.account != "default" else ""
        header = f"🛞 *{tag}Wheel — {len(actions)} order{'s' if len(actions) != 1 else ''} placed*"
        body   = "\n".join(f"• {a}" for a in actions)
        footer = f"Equity: ${equity:,.2f} · Deployed: {exposure}%"
        send_telegram(f"{header}\n{body}\n\n{footer}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
