"""Pure risk/sizing functions. No I/O so they are easy to unit-test."""

from __future__ import annotations


def daily_loss_halt(equity: float, last_equity: float, halt_pct: float) -> bool:
    """True if the account is down more than halt_pct since the prior close."""
    if last_equity <= 0:
        return False
    change = (equity - last_equity) / last_equity
    return change <= -abs(halt_pct)


def deployable_cash(equity: float, cash: float, cash_buffer_pct: float) -> float:
    """Cash we may spend while preserving the equity-based cash buffer.

    Never returns more than `cash` (no margin), never negative.
    """
    min_cash = equity * cash_buffer_pct
    return max(0.0, min(cash, cash - min_cash))


def position_notional(equity: float, max_position_pct: float) -> float:
    """Target dollar size for a single new position."""
    return equity * max_position_pct


def hit_stop_loss(avg_entry: float, price: float, stop_loss_pct: float) -> bool:
    if avg_entry <= 0:
        return False
    return price <= avg_entry * (1 - abs(stop_loss_pct))
