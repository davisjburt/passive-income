"""End-of-week maintenance: ensure no outstanding orders are left open."""

from __future__ import annotations

import logging

from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest

log = logging.getLogger("bot")


def cancel_open_orders(trading, dry_run: bool = False) -> list[str]:
    """Cancel every open (unfilled/partially-filled) order. Returns symbols cancelled.

    Run at week's end so nothing stale sits in the order book over the weekend.
    """
    open_orders = trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
    cancelled = []
    for o in open_orders:
        cancelled.append(o.symbol)
        if dry_run:
            log.info("[dry-run] would cancel open order %s %s", o.symbol, o.id)
            continue
        try:
            trading.cancel_order_by_id(o.id)
            log.info("Cancelled open order %s %s", o.symbol, o.id)
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to cancel order %s (%s): %s", o.symbol, o.id, exc)
    if not cancelled:
        log.info("No outstanding orders to cancel.")
    return cancelled
