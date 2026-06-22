"""
Longshot-fade paper trader.

Forward-tests the one candidate edge from the settled-market backtest: FADE
overpriced longshots by buying NO on markets whose YES price sits in a cheap
band. Logs signals to the existing paper tracker (src/paper/tracker.py) -- no
real orders, no money at risk.

Critically, this enters at the REAL NO ask from the live order book (what you
would actually pay), not a theoretical mid. That immediately measures the
slippage that decides whether the ~1c/contract edge survives in practice.
Settlement P&L accrues later (markets resolve over days) via tracker.settle_signal.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import List, Optional

from src.clients.kalshi_client import KalshiClient
from src.paper import tracker
from src.utils.fees import kalshi_taker_fee

# Rule parameters (from settled_backtest.py best candidate band).
YES_BAND_LO = 0.02     # ignore sub-2c dust
YES_BAND_HI = 0.15
MIN_VOLUME = 500.0
MIN_NO_ASK_SIZE = 1.0
MAX_DAYS_TO_EXPIRY = 45
MAX_SIGNALS = 40       # cap like a small real account


def _f(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


@dataclass
class Candidate:
    ticker: str
    title: str
    yes_mid: float
    no_ask: float          # real price we'd pay for NO
    fair_no: float         # 1 - yes_mid (efficient NO price)
    slippage: float        # no_ask - fair_no (edge eaten by the book)
    size: float
    volume: float


async def scan_candidates(max_markets: int = 8000) -> List[Candidate]:
    client = KalshiClient()
    cands: List[Candidate] = []
    seen = 0
    cursor = None
    import time as _t
    now = _t.time()
    try:
        while seen < max_markets:
            try:
                resp = await client.get_markets(limit=200, cursor=cursor, status="open")
            except Exception as e:
                # Flaky network (recurrent getaddrinfo failures) must not discard a
                # whole scan -- keep what we have and stop gracefully.
                print(f"[scan] network error after {seen} markets ({e}); using partial results.")
                break
            mkts = resp.get("markets", []) or []
            if not mkts:
                break
            for m in mkts:
                seen += 1
                # Longshots often have NO yes-bid, so use last traded price (always
                # populated in bulk) plus the yes ask as the YES reference.
                last = _f(m.get("last_price_dollars"))
                ya = _f(m.get("yes_ask_dollars"))
                no_ask = _f(m.get("no_ask_dollars"))
                size = _f(m.get("no_ask_size_fp")) or _f(m.get("no_ask_size")) or 0.0
                vol = _f(m.get("volume_fp")) or _f(m.get("volume")) or 0.0
                yes_ref = last if (last is not None and last > 0) else ya
                if yes_ref is None or no_ask is None:
                    continue
                if not (YES_BAND_LO <= yes_ref <= YES_BAND_HI):
                    continue
                if not (0.01 < no_ask < 1.0) or size < MIN_NO_ASK_SIZE or vol < MIN_VOLUME:
                    continue
                fair_no = 1.0 - yes_ref
                cands.append(Candidate(
                    ticker=m.get("ticker", "?"),
                    title=(m.get("title", "")[:60]),
                    yes_mid=round(yes_ref, 4),
                    no_ask=round(no_ask, 4),
                    fair_no=round(fair_no, 4),
                    slippage=round(no_ask - fair_no, 4),
                    size=size,
                    volume=vol,
                ))
            cursor = resp.get("cursor")
            if not cursor:
                break
    finally:
        await client.close()
    # Prefer the most liquid, lowest-slippage names.
    cands.sort(key=lambda c: (c.slippage, -c.volume))
    return cands


def log_signals(cands: List[Candidate]) -> int:
    """Log new candidates to the paper tracker, skipping markets already pending."""
    pending = {s["market_id"] for s in tracker.get_pending_signals()}
    logged = 0
    for c in cands:
        if c.ticker in pending:
            continue
        # net edge per contract: we win (1-no_ask) when NO resolves; the rule bets
        # the realized YES rate is below the implied yes_mid. Record fee too.
        fee = kalshi_taker_fee(c.no_ask, 1, "standard")
        tracker.log_signal(
            market_id=c.ticker,
            market_title=c.title,
            side="NO",
            entry_price=c.no_ask,
            confidence=round(1.0 - c.yes_mid, 4),
            reasoning=(
                f"longshot-fade: yes_mid={c.yes_mid:.3f}, NO ask={c.no_ask:.3f}, "
                f"fair_no={c.fair_no:.3f}, slippage={c.slippage:+.3f}, fee={fee:.4f}, vol={c.volume:.0f}"
            ),
            strategy="longshot_fade",
        )
        logged += 1
        if logged >= MAX_SIGNALS:
            break
    return logged


async def _amain():
    cands = await scan_candidates()
    print("=" * 78)
    print("LONGSHOT-FADE PAPER TRADER  (live order book, no real orders)")
    print("=" * 78)
    print(f"Live markets matching rule (yes_mid {YES_BAND_LO}-{YES_BAND_HI}, "
          f"vol>={MIN_VOLUME:.0f}, fillable NO): {len(cands)}")
    if cands:
        avg_slip = sum(c.slippage for c in cands) / len(cands)
        # edge after real ask + fee, betting NO wins (rule assumes longshot misses)
        avg_no_ask = sum(c.no_ask for c in cands) / len(cands)
        avg_fee = sum(kalshi_taker_fee(c.no_ask, 1, "standard") for c in cands) / len(cands)
        # gross if NO always wins = 1 - no_ask; realistic uses the implied yes_mid as hit prob
        avg_yes = sum(c.yes_mid for c in cands) / len(cands)
        ev = (1 - avg_yes) * (1 - avg_no_ask) - avg_yes * avg_no_ask - avg_fee
        print(f"Avg YES mid: {avg_yes:.3f} | Avg NO ask paid: {avg_no_ask:.3f} | "
              f"Avg book slippage vs fair: {avg_slip:+.3f}")
        print(f"Avg fee/contract: ${avg_fee:.4f}")
        print(f"Expected $/contract at REAL asks (using implied hit rate): ${ev:+.4f}")
        print()
        print(f"{'ticker':32s} yes_mid  NO_ask  slip   vol")
        for c in cands[:15]:
            print(f"{c.ticker:32s} {c.yes_mid:6.3f}  {c.no_ask:6.3f} {c.slippage:+5.3f} {c.volume:8.0f}")
        n = log_signals(cands)
        print(f"\nLogged {n} new paper signals (side=NO) to the paper tracker.")
        st = tracker.get_stats()
        print(f"Paper portfolio: {st['total_signals']} signals "
              f"({st['pending']} pending, {st['settled']} settled). "
              f"Settled P&L so far: ${st['total_pnl']:.2f}")
        if ev <= 0:
            print("\nVERDICT: at REAL NO asks the expected edge is <= 0 -> the book")
            print("slippage eats it. The penny edge does NOT survive live quotes. Good")
            print("that we tested before risking money.")
        else:
            print(f"\nVERDICT: edge survives real asks at ~${ev:+.4f}/contract. Signals")
            print("logged; they settle over days. Re-run to settle & add signals.")
    else:
        print("No markets currently match the rule with a fillable NO side.")
    print("=" * 78)


if __name__ == "__main__":
    asyncio.run(_amain())
