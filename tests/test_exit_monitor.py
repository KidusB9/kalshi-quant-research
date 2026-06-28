"""
Regression tests for the exit-monitor overselling / short bug.

The bug: the monitor re-issued a sell every cycle before the prior sell
registered, selling PAST zero into large unintended short positions (e.g.
KXNBA2KCOVER bought 5, sold 73 -> short 68). Root causes: (1) reconstructing
position from the lagging fills feed, (2) reading the wrong field (`position`
instead of `position_fp`). These tests lock in the fix.
"""

import asyncio

from src.strategies.exit_monitor import decide_sells, get_long_positions


def test_sells_exactly_held_never_more():
    pos = {"NBA": {"qty": 5, "resting": 0}}
    sells = decide_sells(pos, {"NBA": 0.89}, {"NBA": 0.70})  # dropped 0.19 >= 0.15
    assert len(sells) == 1
    assert sells[0][0] == "NBA"
    assert sells[0][3] == 5  # exactly the held qty -- can never go short


def test_skips_when_a_sell_is_already_resting():
    # THE BUG itself: a sell is already resting; must NOT place another.
    pos = {"NBA": {"qty": 5, "resting": 1}}
    assert decide_sells(pos, {"NBA": 0.89}, {"NBA": 0.70}) == []


def test_no_sell_on_healthy_position():
    pos = {"MLB": {"qty": 5, "resting": 0}}
    assert decide_sells(pos, {"MLB": 0.85}, {"MLB": 0.84}) == []  # only -0.01


def test_no_sell_into_dead_book():
    pos = {"X": {"qty": 5, "resting": 0}}
    assert decide_sells(pos, {"X": 0.85}, {"X": 0.02}) == []  # below MIN_BID


def test_flat_position_does_nothing():
    pos = {"X": {"qty": 0, "resting": 0}}
    assert decide_sells(pos, {"X": 0.85}, {"X": 0.50}) == []


class _FakeClient:
    async def _make_authenticated_request(self, *a, **k):
        return {"market_positions": [
            {"ticker": "LONG", "position_fp": "5", "resting_orders_count": "0"},
            {"ticker": "SHORT", "position_fp": "-68", "resting_orders_count": "0"},
            {"ticker": "FLAT", "position_fp": "0", "resting_orders_count": "0"},
        ]}


def test_positions_use_position_fp_and_longs_only():
    out = asyncio.run(get_long_positions(_FakeClient()))
    assert out.get("LONG", {}).get("qty") == 5   # reads position_fp correctly
    assert "SHORT" not in out                    # NEVER act on a short (no further overselling)
    assert "FLAT" not in out                     # never act on flat
