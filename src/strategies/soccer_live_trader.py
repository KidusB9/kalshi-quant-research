"""
Real-time soccer in-game convergence trader.

The idea: during a live soccer match, when a team takes a decisive lead (e.g.
3-0 at 75'), the market price for that team jumps toward $1 but can briefly lag
the true probability. Buying that jump while there is still margin in the ask,
and holding to settlement, captures the convergence.

Unlike the general convergence trader (which uses "late in market life"), soccer
game-winner markets on Kalshi stay open until the tournament ends, so lateness
is useless here. Instead this detects an IN-GAME move directly:

  ENTRY (all required):
    * soccer game-winner market, open, NOT a KXMVE micro-market
    * actively traded now (24h volume above a floor -> game is live/imminent)
    * a favorite has emerged: yes mid >= MID_MIN
    * the price JUST jumped: mid is at least JUMP above its previous quote
      (this is the "a goal just went in" signal -- it filters out static
      pre-game favorites and targets the live convergence)
    * the ask still leaves margin after fees, and is fillable
    * we are not already in this game

Run it on a loop during match windows; it checks every POLL_SECONDS. Dry-run by
default; live requires TRADING_HALTED=false and SOCCER_LIVE=true.

HONEST NOTE: the pure in-game edge (price lagging the lead) is less proven than
the settled-market backtest -- live books can already be efficient. The JUMP +
margin filters bias toward genuine lags, but validate at small size first.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import List, Optional

from src.clients.kalshi_client import KalshiClient
from src.utils.fees import kalshi_taker_fee

SOCCER_KEYS = ("WCGAME", "SOCCER", "UCLGAME", "EPLGAME", "MLSGAME", "UEFA", "LIGAGAME")
MID_MIN = float(os.getenv("SOCCER_MID_MIN", "0.80"))
ASK_MAX = float(os.getenv("SOCCER_ASK_MAX", "0.92"))
MIN_MARGIN = float(os.getenv("SOCCER_MIN_MARGIN", "0.05"))
JUMP = float(os.getenv("SOCCER_JUMP", "0.03"))         # min recent upward move in mid
MIN_VOL24 = float(os.getenv("SOCCER_MIN_VOL24", "1000"))
CONTRACTS = int(os.getenv("SOCCER_CONTRACTS", "5"))
MAX_TOTAL = float(os.getenv("SOCCER_MAX_CAPITAL", "20"))
POLL_SECONDS = int(os.getenv("SOCCER_POLL_SECONDS", "90"))


def _f(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _is_soccer_game(tk: str) -> bool:
    t = tk.upper()
    if t.startswith("KXMVE"):
        return False
    return any(k in t for k in SOCCER_KEYS)


async def scan(client: KalshiClient, held: set) -> List[dict]:
    cands = []
    cursor = None
    for _ in range(30):
        try:
            resp = await client.get_events(limit=200, cursor=cursor, status="open", with_nested_markets=True)
        except Exception as e:
            print(f"[soccer] scan stop ({e})")
            break
        evs = resp.get("events", []) or []
        if not evs:
            break
        for e in evs:
            for m in (e.get("markets") or []):
                tk = m.get("ticker", "")
                if not _is_soccer_game(tk) or tk in held:
                    continue
                yb, ya = _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars"))
                v24 = _f(m.get("volume_24h_fp")) or 0.0
                asz = _f(m.get("yes_ask_size_fp")) or 0.0
                if yb is None or ya is None:
                    continue
                mid = (yb + ya) / 2
                pyb, pya = _f(m.get("previous_yes_bid_dollars")), _f(m.get("previous_yes_ask_dollars"))
                prev_mid = ((pyb + pya) / 2) if (pyb and pya) else mid
                jump = mid - prev_mid
                margin = (1.0 - ya) - kalshi_taker_fee(ya, 1, "standard")
                if (mid >= MID_MIN and ya <= ASK_MAX and 0.01 < ya < 0.999 and margin >= MIN_MARGIN
                        and v24 >= MIN_VOL24 and asz >= CONTRACTS and jump >= JUMP):
                    cands.append({"ticker": tk, "title": m.get("title", "")[:40], "mid": round(mid, 3),
                                  "ask": round(ya, 3), "jump": round(jump, 3), "margin": round(margin, 4),
                                  "vol24": v24, "cost": round(ya * CONTRACTS, 2)})
        cursor = resp.get("cursor")
        if not cursor:
            break
    cands.sort(key=lambda c: c["jump"], reverse=True)
    return cands


async def place(client: KalshiClient, c: dict) -> bool:
    try:
        await client.place_order(ticker=c["ticker"], client_order_id=str(uuid.uuid4()), side="yes",
                                 action="buy", count=CONTRACTS, type_="limit",
                                 yes_price=int(round(c["ask"] * 100)))
        return True
    except Exception as e:
        print(f"  order failed {c['ticker']}: {e}")
        return False


async def _cycle(live: bool, held: set, spent: list) -> None:
    client = KalshiClient()
    try:
        cands = await scan(client, held)
        stamp = time.strftime("%H:%M:%S")
        if not cands:
            print(f"[{stamp}] no in-game convergence signals (no live soccer game with a fresh favorite move).")
            return
        print(f"[{stamp}] {len(cands)} signal(s):")
        for c in cands:
            if spent[0] + c["cost"] > MAX_TOTAL:
                print(f"   SKIP {c['ticker']} (cap ${MAX_TOTAL:.0f} reached)")
                continue
            print(f"   {c['ticker']:40s} mid={c['mid']:.2f} ask={c['ask']:.2f} jump=+{c['jump']:.2f} "
                  f"margin=${c['margin']:.3f} vol24={c['vol24']:.0f}")
            if live:
                if await place(client, c):
                    held.add(c["ticker"]); spent[0] += c["cost"]
                    print(f"      -> LIVE BET placed (${c['cost']:.2f}).")
            else:
                held.add(c["ticker"])
    finally:
        await client.close()


async def _main():
    live_flag = os.getenv("SOCCER_LIVE", "false").lower() == "true"
    halted = os.getenv("TRADING_HALTED", "true").lower() in ("1", "true", "yes", "on")
    live = ("--live" in __import__("sys").argv) and live_flag and not halted
    loop = "--loop" in __import__("sys").argv
    print("=" * 80)
    print(f"SOCCER LIVE IN-GAME TRADER  ({'LIVE' if live else 'DRY-RUN'}{' / loop' if loop else ''})")
    print(f"filters: mid>={MID_MIN}, ask<={ASK_MAX}, jump>=+{JUMP}, margin>={MIN_MARGIN}, "
          f"vol24>={MIN_VOL24:.0f}, cap ${MAX_TOTAL:.0f}, {CONTRACTS} contracts")
    if ("--live" in __import__("sys").argv) and not live:
        print("LIVE requested but blocked: set TRADING_HALTED=false AND SOCCER_LIVE=true.")
    print("=" * 80)
    held: set = set()
    spent = [0.0]
    if loop:
        print(f"Looping every {POLL_SECONDS}s. Ctrl+C to stop.\n")
        while spent[0] < MAX_TOTAL:
            await _cycle(live, held, spent)
            await asyncio.sleep(POLL_SECONDS)
        print(f"Capital cap ${MAX_TOTAL:.0f} reached -- stopping.")
    else:
        await _cycle(live, held, spent)
        print("\n(single check. add --loop to monitor continuously during matches.)")


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print("\nstopped.")
