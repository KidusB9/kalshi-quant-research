"""
Live complete-set arbitrage scanner.

This is the ONE edge that is verifiable immediately with no outcome history:
within a Kalshi event whose outcomes are mutually exclusive AND exhaustive,
if you can BUY the YES side of every leg for a combined ask < $1 (net of
fees), exactly one leg pays $1 -> guaranteed profit regardless of which wins.

Unlike the snapshot DB (which stores only a mid, yes+no==1), this hits the
LIVE API for real asks and real available size, and it groups by Kalshi's
actual ``mutually_exclusive`` event flag -- not unreliable ticker-string
splitting. Read-only: places no orders.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from typing import List, Optional

from src.clients.kalshi_client import KalshiClient
from src.utils.fees import kalshi_taker_fee


def _f(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


@dataclass
class ArbHit:
    event_ticker: str
    n_legs: int
    sum_yes_ask: float
    gross_profit: float       # 1 - sum(yes_ask), per 1-contract set
    total_fee: float
    net_profit: float         # after round-trip-free hold-to-expiry entry fees
    min_size: float           # smallest available ask size across legs (sets max scale)


async def scan_complete_set_arbitrage(
    max_events: int = 4000,
    min_leg_size: float = 1.0,
) -> List[ArbHit]:
    """
    Scan live mutually-exclusive events for a buy-all-YES lock after fees.

    Only events flagged mutually_exclusive are considered. A leg counts only
    if it has a real yes ask AND available ask size >= min_leg_size, so a
    "lock" that can't actually be filled is not reported.
    """
    client = KalshiClient()
    hits: List[ArbHit] = []
    scanned = 0
    cursor = None
    try:
        while scanned < max_events:
            resp = await client.get_events(limit=200, cursor=cursor, status="open", with_nested_markets=True)
            events = resp.get("events", []) or []
            if not events:
                break
            for ev in events:
                scanned += 1
                if not ev.get("mutually_exclusive"):
                    continue
                markets = ev.get("markets", []) or []
                if len(markets) < 2:
                    continue

                legs = []
                ok = True
                for m in markets:
                    # Only OPEN legs with a real, fillable ask.
                    if m.get("status") not in (None, "active", "open"):
                        ok = False
                        break
                    ask = _f(m.get("yes_ask_dollars"))
                    if ask is None:
                        ask = _f(m.get("yes_ask"))
                        if ask is not None:
                            ask = ask / 100.0
                    size = _f(m.get("yes_ask_size_fp")) or _f(m.get("yes_ask_size")) or 0.0
                    if ask is None or ask <= 0 or ask >= 1 or size < min_leg_size:
                        ok = False
                        break
                    legs.append((ask, size))
                if not ok or len(legs) < 2:
                    continue

                sum_ask = sum(a for a, _ in legs)
                if sum_ask >= 1.0:
                    continue  # no buy-all-YES lock
                # EXHAUSTIVENESS GUARD: mutually_exclusive != exhaustive. A true
                # complete partition's asks sum to ~1 (plus vig). A sum well below
                # 1 means an UNLISTED outcome can win, so buy-all-YES is NOT a lock
                # -- it's just a longshot field. Require the listed legs to plausibly
                # cover the outcome space (sum >= 0.97) before calling it arbitrage.
                if sum_ask < 0.97:
                    continue
                total_fee = sum(kalshi_taker_fee(a, 1, "standard") for a, _ in legs)
                net = (1.0 - sum_ask) - total_fee
                if net <= 0:
                    continue
                hits.append(ArbHit(
                    event_ticker=ev.get("event_ticker", "?"),
                    n_legs=len(legs),
                    sum_yes_ask=round(sum_ask, 4),
                    gross_profit=round(1.0 - sum_ask, 4),
                    total_fee=round(total_fee, 4),
                    net_profit=round(net, 4),
                    min_size=min(s for _, s in legs),
                ))
            cursor = resp.get("cursor")
            if not cursor:
                break
    finally:
        await client.close()

    hits.sort(key=lambda h: h.net_profit, reverse=True)
    return hits, scanned


async def _main():
    hits, scanned = await scan_complete_set_arbitrage()
    print("=" * 70)
    print("LIVE COMPLETE-SET ARBITRAGE SCAN (real asks, MECE events only)")
    print("=" * 70)
    print(f"Mutually-exclusive events scanned: {scanned}")
    print(f"Real fillable locks found (net > $0 after fees): {len(hits)}\n")
    if not hits:
        print("No risk-free arbitrage available right now. The market is efficient")
        print("on this dimension -- consistent with the research finding. Honest zero.")
    else:
        print("Candidate locks (legs sum to >=0.97, so plausibly exhaustive).")
        print("STILL verify each by hand: confirm no missing 'other/field' leg")
        print("before risking money -- a single unlisted outcome breaks the lock.\n")
        print(f"{'event':40s} legs  sum_ask  net/contract  min_size")
        for h in hits[:25]:
            print(f"{h.event_ticker:40s} {h.n_legs:4d}  {h.sum_yes_ask:7.3f}  ${h.net_profit:+.4f}    {h.min_size:.0f}")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(_main())
