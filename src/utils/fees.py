"""Kalshi fee calculations.

Implements Kalshi's exact fee formulas for taker and maker orders.
Fee per contract = ceil(multiplier * P * (1 - P) * 10000) / 10000,
where P is the contract price in dollars (0.01 to 0.99).

Standard categories (politics, economics, sports, weather, culture)
use a 0.07 taker multiplier; S&P 500 / NASDAQ-100 markets use 0.035.
Maker multiplier is always 1/4 of the taker multiplier.
"""

from __future__ import annotations

import math

# ---------------------------------------------------------------------------
# Fee multipliers by category
# ---------------------------------------------------------------------------

TAKER_MULTIPLIER_STANDARD: float = 0.07
TAKER_MULTIPLIER_INDEX: float = 0.035  # S&P 500 / NASDAQ-100

MAKER_RATIO: float = 0.25  # maker multiplier = taker multiplier * 0.25

STANDARD_CATEGORIES: frozenset[str] = frozenset(
    {"politics", "economics", "sports", "weather", "culture", "standard"}
)
INDEX_CATEGORIES: frozenset[str] = frozenset(
    {"sp500", "nasdaq100", "index"}
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _taker_multiplier(category: str) -> float:
    cat = category.lower().strip()
    if cat in INDEX_CATEGORIES:
        return TAKER_MULTIPLIER_INDEX
    return TAKER_MULTIPLIER_STANDARD


def _fee_per_contract(multiplier: float, price: float) -> float:
    """Kalshi fee for a single contract: ceil(multiplier * P * (1-P) * 10000) / 10000.

    A small round(..., 8) is applied before ceil to neutralise IEEE-754
    representation noise (e.g. 0.07 * 0.5 * 0.5 * 10000 == 175.00000000000003).
    """
    if price <= 0.0 or price >= 1.0:
        return 0.0
    raw = round(multiplier * price * (1.0 - price) * 10_000, 8)
    return math.ceil(raw) / 10_000


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def kalshi_taker_fee(
    price: float, quantity: int = 1, category: str = "standard"
) -> float:
    """Total taker fee in dollars for *quantity* contracts at *price*.

    Args:
        price: Contract price in dollars, 0.01 to 0.99.
        quantity: Number of contracts.
        category: Market category -- one of the STANDARD_CATEGORIES or
            INDEX_CATEGORIES strings.

    Returns:
        Total fee in dollars (always >= 0).
    """
    mult = _taker_multiplier(category)
    return round(_fee_per_contract(mult, price) * quantity, 4)


def kalshi_maker_fee(
    price: float, quantity: int = 1, category: str = "standard"
) -> float:
    """Total maker fee in dollars for *quantity* contracts at *price*.

    Same formula as the taker fee but with multiplier * MAKER_RATIO.
    """
    mult = _taker_multiplier(category) * MAKER_RATIO
    return round(_fee_per_contract(mult, price) * quantity, 4)


def round_trip_cost(
    entry_price: float,
    exit_price: float,
    quantity: int = 1,
    entry_type: str = "taker",
    exit_type: str = "maker",
    category: str = "standard",
) -> float:
    """Total fees for entering and exiting a position.

    Args:
        entry_price: Price paid to enter (0.01-0.99).
        exit_price: Price received on exit (0.01-0.99).
        quantity: Number of contracts.
        entry_type: ``"taker"`` or ``"maker"`` for the entry leg.
        exit_type: ``"taker"`` or ``"maker"`` for the exit leg.
        category: Market category string.

    Returns:
        Combined entry + exit fee in dollars.
    """
    fee_fn_entry = kalshi_taker_fee if entry_type == "taker" else kalshi_maker_fee
    fee_fn_exit = kalshi_taker_fee if exit_type == "taker" else kalshi_maker_fee
    return round(
        fee_fn_entry(entry_price, quantity, category)
        + fee_fn_exit(exit_price, quantity, category),
        4,
    )


def net_edge_after_fees(
    edge: float,
    entry_price: float,
    exit_price: float | None = None,
    quantity: int = 1,
    order_type: str = "taker",
    category: str = "standard",
) -> float:
    """Edge minus estimated fees, in dollars.

    If *exit_price* is ``None`` the position is assumed to be held to
    expiry (no exit fee).

    Args:
        edge: Raw expected edge in dollars (total, not per-contract).
        entry_price: Entry price per contract.
        exit_price: Exit price per contract, or ``None`` for hold-to-expiry.
        quantity: Number of contracts.
        order_type: ``"taker"`` or ``"maker"`` applied to both legs.
        category: Market category string.

    Returns:
        Net edge after subtracting fees.
    """
    fee_fn = kalshi_taker_fee if order_type == "taker" else kalshi_maker_fee
    total_fee = fee_fn(entry_price, quantity, category)
    if exit_price is not None:
        total_fee += fee_fn(exit_price, quantity, category)
    return round(edge - total_fee, 4)


def min_profitable_edge(
    price: float, order_type: str = "maker", category: str = "standard"
) -> float:
    """Minimum per-contract edge (in dollars) to break even after fees.

    Assumes a single entry fee at *price* (hold-to-expiry model). For a
    round-trip, double the result or call ``round_trip_cost`` directly.

    Returns:
        Break-even edge in dollars per contract.
    """
    fee_fn = kalshi_taker_fee if order_type == "taker" else kalshi_maker_fee
    return fee_fn(price, quantity=1, category=category)
