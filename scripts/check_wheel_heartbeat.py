"""Watchdog: alert if a wheel workflow has gone quiet.

Runs independently of the Cloudflare Worker that normally dispatches the wheel
workflows every 5 min -- that's the point. If the Worker's deploy goes stale
(exactly what happened 2026-07-17 to 2026-07-20: wheel-aggressive.yml silently
stopped being dispatched for 3 days while wheel.yml kept running fine), this
watchdog is triggered by GitHub's own cron instead, so a single bad Worker
deploy can't take out the alarm along with the thing it's watching.

For each workflow, checks the most recent run via the GitHub Actions API. If
none has started within STALE_MINUTES, sends a Telegram alert. Exits non-zero
on any stale workflow so the run also shows red in the Actions tab.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root, for `bot.*`

from bot.wheel.accounts import ACCOUNTS  # noqa: E402

REPO = "davisjburt/passive-income"
# Every registered account's trading workflow, plus the one shared recap
# workflow. Derived from bot/wheel/accounts.py so a new account is watched
# automatically -- nothing to remember to add here.
WORKFLOWS = [a.workflow for a in ACCOUNTS] + ["recap.yml"]
STALE_MINUTES = 30  # generous vs. the 5-min dispatch interval to absorb GitHub cron jitter


def _api_get(path: str) -> dict:
    token = os.getenv("GITHUB_TOKEN")
    req = urllib.request.Request(f"https://api.github.com{path}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def last_run_age_minutes(workflow: str) -> float | None:
    """Minutes since the most recent run of `workflow` started, or None if it has never run."""
    data = _api_get(f"/repos/{REPO}/actions/workflows/{workflow}/runs?per_page=1")
    runs = data.get("workflow_runs") or []
    if not runs:
        return None
    created = datetime.fromisoformat(runs[0]["created_at"].replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - created).total_seconds() / 60


def main() -> int:
    from bot.notify import send_telegram

    stale = []
    for wf in WORKFLOWS:
        try:
            age = last_run_age_minutes(wf)
        except (urllib.error.URLError, urllib.error.HTTPError) as exc:
            print(f"{wf}: heartbeat check failed ({exc}) -- treating as OK, will retry next cycle")
            continue

        if age is None:
            print(f"{wf}: no runs found at all")
            stale.append((wf, None))
        elif age > STALE_MINUTES:
            print(f"{wf}: STALE -- last run {age:.0f}m ago (threshold {STALE_MINUTES}m)")
            stale.append((wf, age))
        else:
            print(f"{wf}: OK -- last run {age:.0f}m ago")

    if stale:
        lines = ["\U0001f6a8 *Wheel monitor* -- workflow(s) have gone quiet:"]
        for wf, age in stale:
            detail = "no runs found" if age is None else f"last run {age:.0f}m ago"
            lines.append(f"• `{wf}` -- {detail}")
        lines.append(
            "\nCheck the Cloudflare Worker deploy (`npx wrangler deployments list` "
            "in `worker/` vs `git log -- worker/src/index.js`) and the GitHub Actions "
            "cron fallback."
        )
        send_telegram("\n".join(lines))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
