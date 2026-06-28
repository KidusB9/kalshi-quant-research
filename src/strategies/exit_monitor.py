"""
Stop-loss / dynamic exit monitor.

Sells a position when a favorite turns against you (price drops below entry), to
cut the loss. Example: bought Portugal at 0.85, it falls to 0.68 as a draw
becomes likely -> sell, recover most of the stake.

THIS VERSION FIXES TWO BUGS that caused unintended short positions:
  BUG 1 (overselling): the old version reconstructed holdings from the fills feed
    and re-issued a sell every cycle before the prior sell registered, so it sold
    PAST zero into large shorts. FIX: read the TRUE current position from the
    positions endpoint (`position_fp`), sell ONLY long positions, never more than
    the actual quantity held, and SKIP any market that already has a resting order
    (so it never stacks sells).
  BUG 2 (position misread): the old code read the field `position`; Kalshi's field
    is `position_fp`. FIX: use `position_fp` everywhere.

Guarantees now: never sells a flat/short position, never sells more than held,
never places a second sell while one is resting. Sells are always allowed by the
kill switch and recover cash. Dry-run unless EXIT_LIVE=true.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections import defaultdict
from typing import Dict, Optional, Set

from src.clients.kalshi_client import KalshiClient

STOP_DROP = float(os.getenv("EXIT_STOP_DROP", "0.15"))   # sell if bid fell this far below entry
MIN_BID = float(os.getenv("EXIT_MIN_BID", "0.05"))       # don't dump into a near-dead book


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


async def get_long_positions(client: KalshiClient) -> Dict[str, dict]:
    """TRUE current positions via position_fp. LONG only (qty > 0). Includes the
    count of resting orders so we never stack a second sell."""
    r = await client._make_authenticated_request(
        "GET", "/trade-api/v2/portfolio/positions", params={"limit": 500}, require_auth=True)
    out: Dict[str, dict] = {}
    for p in r.get("market_positions", []):
        q = _f(p.get("position_fp"))
        if q > 0:  # only longs -- NEVER act on a short or flat position
            out[p.get("ticker")] = {"qty": q, "resting": int(_f(p.get("resting_orders_count")))}
    return out


async def get_entry_prices(client: KalshiClient, tickers: Set[str]) -> Dict[str, float]:
    """Average BUY price (cost basis) per ticker from fills, for the given longs."""
    qty: Dict[str, float] = defaultdict(float)
    cost: Dict[str, float] = defaultdict(float)
    cursor = None
    for _ in range(12):
        r = await client._make_authenticated_request(
            "GET", "/trade-api/v2/portfolio/fills",
            params={"limit": 200, **({"cursor": cursor} if cursor else {})}, require_auth=True)
        for fl in r.get("fills", []):
            tk = fl.get("ticker") or fl.get("market_ticker")
            if tk not in tickers or fl.get("side") != "yes" or fl.get("action") != "buy":
                continue
            c = _f(fl.get("count_fp")) or _f(fl.get("count"))
            px = _f(fl.get("yes_price_dollars"))
            if c > 0 and px > 0:
                qty[tk] += c
                cost[tk] += c * px
        cursor = r.get("cursor")
        if not cursor:
            break
    return {tk: cost[tk] / qty[tk] for tk in qty if qty[tk] > 0}


def decide_sells(positions: Dict[str, dict], entries: Dict[str, float],
                 bids: Dict[str, float]) -> list:
    """Pure decision logic (unit-testable). Returns [(ticker, entry, bid, qty)].
    Enforces: skip if a resting order exists; sell qty == held qty (never more);
    only when bid dropped >= STOP_DROP below entry and bid is fillable."""
    out = []
    for tk, info in positions.items():
        if info["resting"] > 0:          # GUARD: never stack a sell -> no overselling
            continue
        qty = int(info["qty"])
        if qty < 1:                      # GUARD: nothing to sell (flat)
            continue
        entry = entries.get(tk)
        bid = bids.get(tk, 0.0)
        if entry is None or bid < MIN_BID:
            continue
        if bid <= entry - STOP_DROP:
            out.append((tk, entry, bid, qty))   # qty is exactly what's held -> never short
    return out


async def run_once(live: bool) -> list:
    client = KalshiClient()
    try:
        positions = await get_long_positions(client)
        entries = await get_entry_prices(client, set(positions))
        bids: Dict[str, float] = {}
        for tk in positions:
            try:
                md = (await client.get_market(tk)).get("market", {})
                bids[tk] = _f(md.get("yes_bid_dollars"))
            except Exception:
                bids[tk] = 0.0
        sells = decide_sells(positions, entries, bids)
        if live:
            for tk, entry, bid, qty in sells:
                try:
                    await client.place_order(
                        ticker=tk, client_order_id=str(uuid.uuid4()), side="yes",
                        action="sell", count=qty, type_="limit",
                        yes_price=max(1, int(round(bid * 100))))
                    print(f"  SOLD {tk} x{qty} at {bid:.2f} (entry {entry:.2f})")
                except Exception as e:
                    print(f"  sell failed {tk}: {e}")
        return sells
    finally:
        await client.close()


def run() -> None:
    live = os.getenv("EXIT_LIVE", "false").lower() == "true"
    acts = asyncio.run(run_once(live))
    if not acts:
        print("exit-monitor: all positions healthy (no stop-loss triggered).")
    else:
        tag = "SOLD" if live else "WOULD SELL (dry-run)"
        for tk, entry, bid, qty in acts:
            print(f"exit-monitor: {tag} {tk} x{qty} bid={bid:.2f} entry={entry:.2f} drop={entry-bid:+.2f}")


if __name__ == "__main__":
    run()
