"""
Paper market-maker forward-test / simulator.

Tests the only strategy the data supports: provide liquidity (post two-sided
quotes), capture the spread + low maker fee, and survive adverse selection.

It uses REAL data:
  * real spreads from the live order book (gross capture),
  * real recent price volatility per market (adverse-selection estimate),
so it produces an HONEST expected net PnL per round-trip without waiting days.
It also logs the chosen quotes to the paper tracker as a record. No real orders.

Net edge per round-trip (per contract pair) is modeled as:
    spread_captured  -  maker_fees(both legs)  -  adverse_selection
where adverse_selection is estimated from the market's own recent price moves
(a passive maker gets picked off roughly in proportion to short-horizon vol).
"""

from __future__ import annotations

import asyncio
import math
import statistics
from dataclasses import dataclass
from typing import List, Optional

from src.clients.kalshi_client import KalshiClient
from src.paper import tracker


def maker_fee(p: float) -> float:
    if p <= 0 or p >= 1:
        return 0.0
    return 0.25 * math.ceil(0.07 * p * (1 - p) * 10000) / 10000


def _f(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


@dataclass
class MMCandidate:
    ticker: str
    title: str
    bid: float
    ask: float
    spread: float
    mid: float
    volume: float
    prev_mid: Optional[float] = None  # previous quote mid (for real move estimate)
    adverse: Optional[float] = None   # estimated adverse-selection cost per round-trip
    net_ev: Optional[float] = None    # spread - fees - adverse


async def find_candidates(max_events: int = 6000) -> List[MMCandidate]:
    client = KalshiClient()
    cands: List[MMCandidate] = []
    cursor = None
    scanned = 0
    try:
        while scanned < max_events:
            try:
                resp = await client.get_events(limit=200, cursor=cursor, status="open", with_nested_markets=True)
            except Exception as e:
                print(f"[mm] network stop after {scanned} events ({e})")
                break
            evs = resp.get("events", []) or []
            if not evs:
                break
            for e in evs:
                scanned += 1
                for m in (e.get("markets") or []):
                    tk = m.get("ticker", "")
                    if tk.upper().startswith("KXMVE"):
                        continue
                    yb = _f(m.get("yes_bid_dollars"))
                    ya = _f(m.get("yes_ask_dollars"))
                    vol = _f(m.get("volume_fp")) or 0.0
                    vol24 = _f(m.get("volume_24h_fp")) or 0.0
                    if yb is None or ya is None or not (0.02 < yb < ya < 0.98):
                        continue
                    spread = ya - yb
                    # A capturable spread is TIGHT (2-10c) on a market that is
                    # ACTUALLY trading right now. Wide "spreads" are dead books
                    # (lowball bid vs highball ask) you can never capture, and
                    # they read ~0 adverse because nobody trades them -- pure junk.
                    if not (0.02 <= spread <= 0.10) or vol < 1000 or vol24 < 100:
                        continue
                    pyb = _f(m.get("previous_yes_bid_dollars"))
                    pya = _f(m.get("previous_yes_ask_dollars"))
                    prev_mid = None
                    if pyb is not None and pya is not None and 0 < pyb <= pya < 1:
                        prev_mid = (pyb + pya) / 2
                    cands.append(MMCandidate(
                        ticker=tk, title=m.get("title", "")[:55],
                        bid=round(yb, 4), ask=round(ya, 4), spread=round(spread, 4),
                        mid=round((yb + ya) / 2, 4), volume=vol,
                        prev_mid=(round(prev_mid, 4) if prev_mid is not None else None),
                    ))
            cursor = resp.get("cursor")
            if not cursor:
                break
    finally:
        await client.close()
    cands.sort(key=lambda c: c.volume, reverse=True)
    return cands


def estimate_adverse(cands: List[MMCandidate]) -> None:
    """
    Estimate adverse selection from REAL recent price movement: how far the mid
    moved from its previous quote (|mid - prev_mid|). A passive maker gets picked
    off roughly in proportion to how much price moves while its quote rests.

    This is a ONE-STEP proxy (lower bound on true adverse selection over a full
    quote lifetime); markets with no prior quote are left unscored rather than
    fabricated.
    """
    for c in cands:
        if c.prev_mid is None:
            continue
        c.adverse = round(abs(c.mid - c.prev_mid), 4)
        fees = maker_fee(c.bid) + maker_fee(c.ask)
        c.net_ev = round(c.spread - fees - c.adverse, 4)


def log_paper_quotes(cands: List[MMCandidate], top: int = 20) -> int:
    """Record the chosen MM quotes to the paper tracker for posterity."""
    logged = 0
    scored = [c for c in cands if c.net_ev is not None]
    for c in sorted(scored, key=lambda x: x.net_ev, reverse=True)[:top]:
        tracker.log_signal(
            market_id=c.ticker, market_title=c.title, side="NO",
            entry_price=c.bid, confidence=round(c.mid, 4),
            reasoning=(f"market-make: bid={c.bid:.3f}/ask={c.ask:.3f} spread={c.spread:.3f} "
                       f"adverse={c.adverse:.3f} net_ev={c.net_ev:+.4f} vol={c.volume:.0f}"),
            strategy="market_making",
        )
        logged += 1
    return logged


async def _main():
    print("=" * 80)
    print("PAPER MARKET-MAKER  (real spreads, real volatility, no real orders)")
    print("=" * 80)
    cands = await find_candidates()
    print(f"Liquid wide-spread candidates (non-MVE, vol>=1000, spread>=2c): {len(cands)}")
    if not cands:
        print("None found. Done.")
        return
    estimate_adverse(cands)
    scored = [c for c in cands if c.net_ev is not None]
    pos = [c for c in scored if c.net_ev > 0]
    print(f"{len(scored)} markets scored using REAL recent price moves (prev->current mid).\n")
    print(f"{'ticker':30s} bid   ask   spread  adverse  NET_EV   vol")
    for c in sorted(scored, key=lambda x: x.net_ev, reverse=True)[:15]:
        print(f"{c.ticker:30s} {c.bid:.3f} {c.ask:.3f}  {c.spread*100:4.1f}c   "
              f"{c.adverse*100:4.1f}c   {c.net_ev*100:+5.2f}c  {c.volume:8.0f}")
    if scored:
        med = statistics.median(c.net_ev for c in scored)
        avg = statistics.mean(c.net_ev for c in scored)
        med_sp = statistics.median(c.spread for c in scored)
        med_adv = statistics.median(c.adverse for c in scored)
        print(f"\nMedian spread: {med_sp*100:.1f}c | Median adverse selection: {med_adv*100:.1f}c")
        print(f"NET EV per round-trip -> median: {med*100:+.2f}c | mean: {avg*100:+.2f}c")
        print(f"Markets with positive net EV: {len(pos)}/{len(scored)} ({100*len(pos)/len(scored):.0f}%)")
        n = log_paper_quotes(cands, top=20)
        st = tracker.get_stats()
        print(f"Logged {n} paper MM quotes. Paper portfolio: {st['total_signals']} signals.")
        print("\n" + "=" * 80)
        print("FINAL VERDICT")
        print("=" * 80)
        print("(adverse selection = real one-step mid move; a LOWER BOUND on the true")
        print(" pick-off cost over a full quote life, so treat net EV as optimistic.)\n")
        if med > 0.005:
            print(f"Market-making clears even this proxy: ~{med*100:+.2f}c/round-trip net.")
            print("Wide spreads beat one-step price moves on most markets -> genuinely")
            print("promising. Definitive proof needs forward paper fills over days.")
        elif med > 0:
            print(f"Market-making is MARGINAL: ~{med*100:+.2f}c/round-trip on a LOWER-BOUND")
            print("adverse estimate -- the true cost is higher, so real net is likely ~0 or")
            print("negative except on the widest-spread, slowest markets. Selective at best.")
        else:
            print(f"Market-making is NEGATIVE even before full adverse selection (~{med*100:+.2f}c):")
            print("the spread does not cover price movement. Not an edge.")
        print("=" * 80)


if __name__ == "__main__":
    asyncio.run(_main())
