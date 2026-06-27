"""
Convergence trader.

Implements the one real, fillable edge found in backtesting: high-confidence
sports/event markets are underpriced and converge to a "yes" resolution, so
buying them late in their life when there is still real margin in the ask, and
holding to settlement, is positive expected value.

Backtest summary (300 real settled sports markets, real price paths, real fees):
buying "yes" at the ask when the mid first crosses 0.80-0.85 in the late portion
of the market's life won 97-100% and returned roughly +0.04 to +0.065 per
contract after fees. The honest caveats: the margin is only a few cents, the
rare reversal loses most of the stake, and the backtest sample has correlated
outcomes, so this needs live validation at small size.

Entry rules (all must hold):
  * sports/event market, open, not a KXMVE micro-market
  * yes mid >= MID_MIN (market is confident)
  * yes ask <= ASK_MAX (real margin left: at least ~10 cents of upside)
  * margin after fee >= MIN_MARGIN
  * late in life: elapsed fraction of (open -> close) >= MIN_LIFE_FRAC
  * fillable: ask size >= contracts wanted, and volume above a floor
  * one position per correlated family (same event prefix)

Default mode is DRY-RUN. Live requires TRADING_HALTED=false and CONV_LIVE=true.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

from src.clients.kalshi_client import KalshiClient
from src.utils.fees import kalshi_taker_fee

SPORT_KEYS = ("WC", "NFL", "NBA", "MLB", "NHL", "NCAA", "SOCCER", "UCL", "EPL",
              "TENNIS", "UFC", "GOLF", "PGA", "F1", "WNBA")

MID_MIN = float(os.getenv("CONV_MID_MIN", "0.80"))
ASK_MAX = float(os.getenv("CONV_ASK_MAX", "0.90"))
MIN_MARGIN = float(os.getenv("CONV_MIN_MARGIN", "0.04"))   # net cents after fee
MIN_LIFE_FRAC = float(os.getenv("CONV_MIN_LIFE", "0.5"))
MIN_VOLUME = float(os.getenv("CONV_MIN_VOL", "500"))
CONTRACTS = int(os.getenv("CONV_CONTRACTS", "5"))
MAX_TOTAL = float(os.getenv("CONV_MAX_CAPITAL", "40"))
MAX_POSITIONS = int(os.getenv("CONV_MAX_POS", "10"))


def _f(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _ts(iso: Optional[str]) -> Optional[int]:
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except (ValueError, AttributeError):
        return None


def _is_sport(tk: str) -> bool:
    t = tk.upper()
    return (not t.startswith("KXMVE")) and any(k in t for k in SPORT_KEYS)


def _family(tk: str) -> str:
    # group correlated markets (same player/event, different dates/strikes)
    parts = tk.split("-")
    return "-".join(parts[:2]) if len(parts) >= 2 else tk


@dataclass
class Entry:
    ticker: str
    title: str
    bid: float
    ask: float
    mid: float
    life_frac: float
    margin_after_fee: float
    ask_size: float
    volume: float
    cost: float


async def find_entries() -> List[Entry]:
    client = KalshiClient()
    entries: List[Entry] = []
    # Seed dedup with already-held families (passed by the autonomous runner) so
    # repeated runs never re-buy the same market.
    families = {_family(t) for t in os.getenv("RUNNER_SKIP_TICKERS", "").split(",") if t}
    capital = 0.0
    cursor = None
    now = time.time()
    try:
        while capital < MAX_TOTAL and len(entries) < MAX_POSITIONS:
            try:
                resp = await client.get_events(limit=200, cursor=cursor, status="open", with_nested_markets=True)
            except Exception as e:
                print(f"[conv] network stop ({e})")
                break
            evs = resp.get("events", []) or []
            if not evs:
                break
            for e in evs:
                for m in (e.get("markets") or []):
                    tk = m.get("ticker", "")
                    if not _is_sport(tk):
                        continue
                    yb = _f(m.get("yes_bid_dollars"))
                    ya = _f(m.get("yes_ask_dollars"))
                    asz = _f(m.get("yes_ask_size_fp")) or 0.0
                    vol = _f(m.get("volume_fp")) or 0.0
                    if yb is None or ya is None:
                        continue
                    mid = (yb + ya) / 2
                    if mid < MID_MIN or ya > ASK_MAX or not (0.01 < ya < 0.999):
                        continue
                    margin = (1.0 - ya) - kalshi_taker_fee(ya, 1, "standard")
                    if margin < MIN_MARGIN or asz < CONTRACTS or vol < MIN_VOLUME:
                        continue
                    # late-life filter
                    open_ts, close_ts = _ts(m.get("open_time")), _ts(m.get("close_time"))
                    if open_ts and close_ts and close_ts > open_ts:
                        frac = (now - open_ts) / (close_ts - open_ts)
                    else:
                        frac = 1.0
                    if frac < MIN_LIFE_FRAC:
                        continue
                    fam = _family(tk)
                    if fam in families:
                        continue
                    cost = ya * CONTRACTS
                    if capital + cost > MAX_TOTAL:
                        continue
                    families.add(fam)
                    capital += cost
                    entries.append(Entry(
                        ticker=tk, title=m.get("title", "")[:46], bid=round(yb, 3), ask=round(ya, 3),
                        mid=round(mid, 3), life_frac=round(min(1, frac), 2),
                        margin_after_fee=round(margin, 4), ask_size=asz, volume=vol, cost=round(cost, 2),
                    ))
                    if len(entries) >= MAX_POSITIONS or capital >= MAX_TOTAL:
                        break
                if len(entries) >= MAX_POSITIONS or capital >= MAX_TOTAL:
                    break
            cursor = resp.get("cursor")
            if not cursor:
                break
    finally:
        await client.close()
    return entries


async def deploy(entries: List[Entry], live: bool) -> None:
    conv_live = os.getenv("CONV_LIVE", "false").lower() == "true"
    halted = os.getenv("TRADING_HALTED", "true").lower() in ("1", "true", "yes", "on")
    if live and (not conv_live or halted):
        print("LIVE blocked: set CONV_LIVE=true AND TRADING_HALTED=false to place real orders.")
        live = False
    if not live:
        print("\nDRY-RUN: no orders placed.")
        return
    client = KalshiClient()
    placed = 0
    try:
        for en in entries:
            try:
                await client.place_order(
                    ticker=en.ticker, client_order_id=str(uuid.uuid4()), side="yes",
                    action="buy", count=CONTRACTS, type_="limit", yes_price=int(round(en.ask * 100)),
                )
                placed += 1
            except Exception as e:
                print(f"  order failed {en.ticker}: {e}")
    finally:
        await client.close()
    print(f"\nLIVE: placed {placed} buy orders. Hold to settlement.")


async def _main():
    print("=" * 84)
    print("CONVERGENCE TRADER  (buy high-confidence sports favorites with margin, hold to settle)")
    print("=" * 84)
    entries = await find_entries()
    if not entries:
        print("No markets currently fit the entry rules. Try later or loosen filters.")
        return
    exp_per = sum(e.margin_after_fee for e in entries) / len(entries)
    print(f"Entries: {len(entries)} | total cost: ${sum(e.cost for e in entries):.2f} | "
          f"avg margin-after-fee/contract: ${exp_per:.4f}")
    print(f"(filters: mid>={MID_MIN}, ask<={ASK_MAX}, margin>={MIN_MARGIN}, life>={MIN_LIFE_FRAC}, "
          f"vol>={MIN_VOLUME:.0f}, {CONTRACTS} contracts, cap ${MAX_TOTAL:.0f})\n")
    print(f"{'ticker':38s} bid  ask  mid  life margin/ct size  cost")
    for e in entries:
        print(f"{e.ticker:38s} {e.bid:.2f} {e.ask:.2f} {e.mid:.2f} {e.life_frac:.2f}  "
              f"${e.margin_after_fee:+.3f}  {e.ask_size:4.0f}  ${e.cost:.2f}")
    await deploy(entries, live=("--live" in __import__("sys").argv))
    print("\nExpected value is positive in backtest but THIN. Each win pays the margin")
    print("above; a rare reversal loses the full ask. Go live small and track the")
    print("realized win rate against the backtest's 97-100% before scaling.")
    print("=" * 84)


if __name__ == "__main__":
    asyncio.run(_main())
