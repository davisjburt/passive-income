"""Pure dual/absolute-momentum logic: given trailing returns, decide what to
hold. No I/O here -- price fetching and order placement live in engine.py.
Mirrors bot/wheel/strategy.py's split so this is fully unit-testable without
touching Alpaca.
"""

from __future__ import annotations


def trailing_return(price_now: float, price_then: float) -> float:
    """Total return from `price_then` to `price_now`."""
    if price_then <= 0:
        return 0.0
    return price_now / price_then - 1


def choose_holding(risky_returns: dict[str, float], safe_symbol: str, safe_return: float) -> str:
    """Dual/absolute momentum: hold whichever risky asset has the strongest
    trailing return, but only if that return also beats the safe asset's own
    trailing return (the "absolute momentum" filter, which is what sidesteps
    broad bear markets rather than just picking the least-bad risky asset).
    Otherwise hold the safe asset.

    risky_returns: {symbol: trailing_return} for every candidate risky asset.
    Returns the symbol to hold for the next period. A tie (vanishingly
    unlikely with real prices) goes to whichever symbol comes first in
    risky_returns' iteration order, i.e. however the caller listed its
    universe -- config.momentum.yaml's risky_universe order, in practice.
    """
    if not risky_returns:
        return safe_symbol
    best_symbol = max(risky_returns, key=risky_returns.get)
    if risky_returns[best_symbol] > safe_return:
        return best_symbol
    return safe_symbol
