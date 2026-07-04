# Changelog

Log of automated/manual tuning decisions for the trading bot(s) in this repo.

## 2026-07-03

Weekly automated review (`weekly-trading-bot-review` scheduled task) ran today. Its
instructions were written for the old RSI(2) ETF bot (`weekly.py`, `config.yaml`,
`bot/strategy.py`, `bot/risk.py`, `docs/reports/`), which was fully removed in
commit `6a1e9d0` ("Remove RSI bot; wheel-only dashboard and tests"). That job no
longer exists — the project is now exclusively the **options wheel** strategy in
`bot/wheel/` (`config.wheel.yaml`, `wheel_run.py`, ledger at
`docs/wheel_ledger.json` / `docs/wheel.json`), running continuously every 5
minutes during market hours (Cloudflare Worker + `.github/workflows/wheel.yml`),
still 100% Alpaca **paper** trading.

Adapted review performed instead:
- Confirmed no outstanding pending orders (`docs/wheel.json.pending_orders` is
  empty as of the latest committed snapshot, 2026-07-03).
- Ran `.venv/bin/python -m pytest -q` — 15/15 tests passed.
- Reviewed current state: equity $9,962.11, exposure 46.2% (within the 70%
  portfolio cap), 3 open short puts (F, SOFI, T) with small unrealized P&L
  (-$4, +$6, -$30), 2 tickers (PFE, KEY) still in CASH state (no position
  opened yet), total premium collected $128, total realized P&L $82.
- **No config change made.** The wheel strategy has only been live for a few
  days and positions run 30-45 DTE, so not even one full cycle has completed —
  far too little data to tune anything, and no authorized guardrail bounds
  exist yet for wheel-specific parameters (`per_stock_cap_pct`,
  `portfolio_wheel_cap_pct`, OTM bands, DTE, min yield). Default-to-no-change
  applies even more strongly here than for the old weekly-bar strategy.

**Action needed from the user:** the `weekly-trading-bot-review` scheduled
task's instructions should be rewritten for the wheel architecture (or
retired), and `README.md` still describes the deleted RSI bot and should be
updated to describe the wheel strategy instead.
