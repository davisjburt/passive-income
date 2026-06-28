# passive-income — conservative RSI(2) mean-reversion bot (Alpaca paper)

An autonomous, **paper-money** trading bot. It buys deeply oversold but still
up-trending ETFs and sells when they bounce, with hard risk guardrails. Designed
to run hands-off for free via GitHub Actions.

> ⚠️ **Read this first.** No trading bot reliably beats a simple index fund. This
> is for learning and experimentation on a **paper** account. There is no path to
> real money without a deliberate code change. Don't fund this with money you
> can't afford to lose, and don't assume past behavior predicts future returns.

## Strategy

- **Universe:** liquid broad ETFs (`SPY QQQ IWM DIA EFA EEM`).
- **Buy** when price is **above its 200-day SMA** (long-term uptrend) **and**
  `RSI(2) < 10` (deeply oversold).
- **Sell** when `RSI(2)` recovers above 60, or price closes back above its 5-day
  SMA, or a stop-loss / time-stop fires.
- **Long-only, cash-only, no margin.**

## Risk guardrails

| Guardrail | Default | Where |
|---|---|---|
| Cash buffer (never fully invested) | 25% of equity | `config.yaml` |
| Max position size | 20% of equity | `config.yaml` |
| Max concurrent positions | 3 | `config.yaml` |
| Per-position stop-loss | 8% | `config.yaml` |
| Time-stop | 10 days | `config.yaml` |
| Daily-loss kill switch (no new buys) | −4% on the day | `config.yaml` |
| Paper-only | hardcoded | `bot/config.py` |

## Local setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# Put credentials in .env (already done if you see ALPACA_API_KEY there):
#   ALPACA_API_KEY=...
#   ALPACA_API_SECRET=...

.venv/bin/python check_connection.py     # verify credentials
.venv/bin/python -m pytest -q            # run tests
.venv/bin/python run.py --dry-run        # see intended trades, submit nothing
.venv/bin/python run.py                  # live paper cycle (only acts if market open)
```

## Autonomous runs (GitHub Actions, free)

1. Create a GitHub repo and push this project.
2. In the repo: **Settings → Secrets and variables → Actions → New repository secret**, add:
   - `ALPACA_API_KEY`
   - `ALPACA_API_SECRET`
3. The workflow in `.github/workflows/trade.yml` runs Mon–Fri at **19:30 UTC** and
   on manual dispatch (**Actions** tab → *trade* → *Run workflow*).
4. The bot checks the market clock and does nothing when the market is closed.

**Note on timing & cron:** `19:30 UTC` is ~30 min before the US close in summer
(EDT) and ~90 min before in winter (EST). GitHub may delay scheduled runs by a few
minutes. Mean-reversion signals are computed on daily bars, so near-close timing is
fine; adjust the cron in the workflow if you want it tighter to the close.

**Data:** uses Alpaca's free IEX feed for daily bars — sufficient for this daily
strategy. No paid data subscription required.

## Tuning

Everything tunable lives in `config.yaml` — universe, RSI thresholds, trend filter,
and all risk caps. No code changes needed.

## Project layout

```
config.yaml            strategy + risk settings (edit this)
run.py                 entry point (one cycle)
check_connection.py    credential smoke test
bot/
  config.py            load config + env, enforce paper-only / no-margin
  client.py            Alpaca client factories
  data.py              daily bars + RSI/SMA indicators
  strategy.py          buy/sell/hold signal logic
  risk.py              pure sizing & guardrail math (unit-tested)
  trader.py            orchestration: state -> signals -> orders
tests/                 unit tests (no network)
.github/workflows/     scheduled CI run
```

## Going to real money (don't rush this)

Paper-trade for **months** and review results before even considering it. Switching
to live requires editing `paper=True` in `bot/config.py` and using live API keys —
intentionally a manual code change, not a config flag.
