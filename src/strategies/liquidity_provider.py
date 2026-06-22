"""
Liquidity Incentive Program (LIP) harvester.

Kalshi's LIP (live through 2026-09-01) pays you for RESTING orders near the best
bid/ask -- you earn a share of a daily reward pool based on order size x proximity
to best price, scored on per-second snapshots, whether or not your order fills.

This is the only positive-EV edge we found: the reward pool pays down the
adverse-selection cost that makes plain market-making break-even.

Strategy (mechanical, NOT prediction):
  1. Select markets that are liquid, tight-spread, and SLOW-moving (low recent
     price movement => low fill/adverse-selection risk while resting).
  2. Post small resting orders at the best bid and best ask (full proximity
     credit) on both sides, within hard capital and per-market caps.
  3. Refresh as best prices move; pull orders from markets that move too fast.

HONEST LIMITS:
  * The reward SCHEDULE/pool size is not in the API I can read, so this targets
    liquid markets where pools tend to concentrate -- it cannot guarantee a
    given market is in an active reward period.
  * Earning real rewards REQUIRES real resting orders => real money, real fill
    risk. Profit = rewards earned - losses from adverse fills. For a small
    account the reward SHARE is small (you compete with bigger LPs).
  * Default mode is DRY-RUN (plans orders, places nothing). Live placement
    requires BOTH env flags set deliberately: LIP_LIVE=true and
    TRADING_HALTED=false. This module never flips those for you.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass
from typing import List, Optional

from src.clients.kalshi_client import KalshiClient


# ---- Risk caps (conservative defaults; override via env) -------------------
MAX_TOTAL_CAPITAL = float(os.getenv("LIP_MAX_CAPITAL", "100"))     # $ at risk across all quotes
MAX_PER_MARKET = float(os.getenv("LIP_MAX_PER_MARKET", "10"))      # $ per market
CONTRACTS_PER_QUOTE = int(os.getenv("LIP_CONTRACTS", "5"))         # contracts per resting order
MAX_MARKETS = int(os.getenv("LIP_MAX_MARKETS", "10"))
MAX_SPREAD = 0.05          # only tight books (cheap to quote at best)
MAX_RECENT_MOVE = 0.02     # only slow markets (<=2c since last quote) -> low fill risk
MIN_VOL_24H = 500.0        # must be actively trading now


def _f(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


@dataclass
class Quote:
    ticker: str
    title: str
    yes_bid: float
    yes_ask: float
    spread: float
    recent_move: float
    # planned resting orders (price in cents for the API)
    bid_price_c: int
    ask_price_c: int
    contracts: int
    capital_at_risk: float


async def select_and_plan() -> List[Quote]:
    """Pick slow, tight, liquid markets and plan best-bid/best-ask resting orders."""
    client = KalshiClient()
    quotes: List[Quote] = []
    cursor = None
    capital_used = 0.0
    try:
        while len(quotes) < MAX_MARKETS and capital_used < MAX_TOTAL_CAPITAL:
            try:
                resp = await client.get_events(limit=200, cursor=cursor, status="open", with_nested_markets=True)
            except Exception as e:
                print(f"[lip] network stop ({e})")
                break
            evs = resp.get("events", []) or []
            if not evs:
                break
            for e in evs:
                for m in (e.get("markets") or []):
                    tk = m.get("ticker", "")
                    if tk.upper().startswith("KXMVE"):
                        continue
                    yb = _f(m.get("yes_bid_dollars"))
                    ya = _f(m.get("yes_ask_dollars"))
                    v24 = _f(m.get("volume_24h_fp")) or 0.0
                    if yb is None or ya is None or not (0.02 < yb < ya < 0.98):
                        continue
                    spread = ya - yb
                    pyb = _f(m.get("previous_yes_bid_dollars"))
                    pya = _f(m.get("previous_yes_ask_dollars"))
                    mid = (yb + ya) / 2
                    prev_mid = ((pyb + pya) / 2) if (pyb and pya) else mid
                    move = abs(mid - prev_mid)
                    if spread > MAX_SPREAD or move > MAX_RECENT_MOVE or v24 < MIN_VOL_24H:
                        continue

                    # Rest at the current best bid and best ask (full proximity credit).
                    bid_c = int(round(yb * 100))
                    ask_c = int(round(ya * 100))
                    if not (1 <= bid_c < ask_c <= 99):
                        continue
                    # Capital at risk if BOTH sides fill = bid cost + (1-ask) cost per pair.
                    cap = (bid_c / 100.0 + (100 - ask_c) / 100.0) * CONTRACTS_PER_QUOTE
                    if cap > MAX_PER_MARKET or capital_used + cap > MAX_TOTAL_CAPITAL:
                        continue
                    capital_used += cap
                    quotes.append(Quote(
                        ticker=tk, title=m.get("title", "")[:50],
                        yes_bid=round(yb, 3), yes_ask=round(ya, 3), spread=round(spread, 3),
                        recent_move=round(move, 3), bid_price_c=bid_c, ask_price_c=ask_c,
                        contracts=CONTRACTS_PER_QUOTE, capital_at_risk=round(cap, 2),
                    ))
                    if len(quotes) >= MAX_MARKETS or capital_used >= MAX_TOTAL_CAPITAL:
                        break
                if len(quotes) >= MAX_MARKETS or capital_used >= MAX_TOTAL_CAPITAL:
                    break
            cursor = resp.get("cursor")
            if not cursor:
                break
    finally:
        await client.close()
    return quotes


async def deploy(quotes: List[Quote], live: bool) -> None:
    """Place the planned resting orders. Live only when explicitly enabled."""
    lip_live = os.getenv("LIP_LIVE", "false").lower() == "true"
    halted = os.getenv("TRADING_HALTED", "true").lower() in ("1", "true", "yes", "on")
    if live and (not lip_live or halted):
        print("LIVE blocked: set LIP_LIVE=true AND TRADING_HALTED=false to place real orders.")
        live = False

    if not live:
        print("\nDRY-RUN -- no orders placed. Plan above is what WOULD be posted.")
        return

    client = KalshiClient()
    placed = 0
    try:
        for q in quotes:
            for side_action in (("yes", q.bid_price_c), ("no", 100 - q.ask_price_c)):
                side, price_c = side_action
                try:
                    await client.place_order(
                        ticker=q.ticker, client_order_id=str(uuid.uuid4()),
                        side=side, action="buy", count=q.contracts,
                        type_="limit", **({"yes_price": price_c} if side == "yes" else {"no_price": price_c}),
                    )
                    placed += 1
                except Exception as e:
                    print(f"  order failed {q.ticker} {side}@{price_c}c: {e}")
    finally:
        await client.close()
    print(f"\nLIVE: placed {placed} resting orders. Monitor fills and rewards on Kalshi.")


async def _main():
    print("=" * 78)
    print("LIQUIDITY INCENTIVE (LIP) HARVESTER")
    print("=" * 78)
    quotes = await select_and_plan()
    if not quotes:
        print("No markets currently fit the slow/tight/liquid filter. Try later or loosen caps.")
        return
    total_cap = sum(q.capital_at_risk for q in quotes)
    print(f"Planned resting quotes: {len(quotes)} markets | capital at risk if all fill: ${total_cap:.2f}")
    print(f"(caps: ${MAX_TOTAL_CAPITAL:.0f} total, ${MAX_PER_MARKET:.0f}/market, {CONTRACTS_PER_QUOTE} contracts/order)\n")
    print(f"{'ticker':30s} bid  ask  spr  move  contracts  $risk")
    for q in quotes:
        print(f"{q.ticker:30s} {q.bid_price_c:3d}c {q.ask_price_c:3d}c {q.spread*100:3.0f}c {q.recent_move*100:3.0f}c   "
              f"{q.contracts:5d}    ${q.capital_at_risk:.2f}")
    await deploy(quotes, live=("--live" in os.sys.argv))
    print("\n" + "=" * 78)
    print("HONEST EXPECTATION: this posts resting orders to earn a SHARE of LIP reward")
    print("pools. Earnings scale with your size vs total LP liquidity (small account =")
    print("small share) and are offset by losses if your orders get filled and the market")
    print("moves. It is positive-EV ONLY if rewards > fill losses -- provable only by")
    print("running live small and measuring actual reward payouts vs fills.")
    print("=" * 78)


if __name__ == "__main__":
    asyncio.run(_main())
