"""Registry of every wheel account/strategy. Add a new one here and it's
automatically picked up by the recap watchdog (scripts/send_recap.py) and by
the --account default-config resolution in wheel_run.py/reset_paper.py --
nothing else in the pipeline needs editing to know a new account exists.

Onboarding a new account still needs, outside this file: its own
config.wheel.<slug>.yaml, its own ALPACA_<SLUG>_API_KEY/SECRET repo secrets,
and its own trading workflow (copy wheel-aggressive.yml). That's inherent to
GitHub Actions secrets being declared per-workflow -- there's no way around
naming them somewhere. What this registry buys you: the recap workflow (which
DOES need every account's credentials, since it reports on all of them) reads
this list instead of anyone hand-editing per-account logic into it, and no
trading workflow ever needs to know another account exists.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AccountInfo:
    slug: str      # matches --account; "default" reads ALPACA_API_KEY/SECRET,
                   # anything else reads ALPACA_<SLUG>_API_KEY/SECRET
    config: str    # path to its config.wheel*.yaml, relative to repo root
    label: str     # human-friendly name for the recap message
    workflow: str  # its trading workflow filename under .github/workflows/,
                   # so tooling (the heartbeat monitor) can derive what to
                   # watch from this registry instead of a separately
                   # hand-maintained list


ACCOUNTS: list[AccountInfo] = [
    AccountInfo(slug="default", config="config.wheel.yaml",
                label="Conservative · $10k", workflow="wheel.yml"),
    AccountInfo(slug="aggressive", config="config.wheel.aggressive.yaml",
                label="Aggressive · $100k", workflow="wheel-aggressive.yml"),
]


def get_account(slug: str) -> AccountInfo:
    for a in ACCOUNTS:
        if a.slug == slug:
            return a
    known = ", ".join(a.slug for a in ACCOUNTS)
    raise KeyError(f"Unknown account slug {slug!r}. Registered in bot/wheel/accounts.py: {known}")
