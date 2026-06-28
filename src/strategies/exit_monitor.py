"""
Stop-loss / dynamic exit monitor.

Watches every open position and SELLS immediately when a bet has turned against
you -- e.g. you bought Portugal to win at 0.85 and the price falls because a draw
or a Colombia win has become likely. Cutting at, say, 0.68 recovers most of the
stake instead of risking the whole thing at settlement.

It reconstructs holdings and entry prices from the fills endpoint (the positions
endpoint returns nothing usable here), gets each market's current bid (what you
can sell into), and sells when:
    current_bid <= entry_price - STOP_DROP   (the lead is slipping)

Selling is always allowed (the TRADING_HALTED kill switch only blocks buys), so
this protective exit runs even when new entries are paused. Dry-run by default;
live requires EXIT_LIVE=true.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections import defaultdict
from typing import Dict, Optional

from src.clients.kalshi_client import KalshiClient

STOP_DROP = float(os.getenv("EXIT_STOP_DROP", "0.15"))   # sell if bid fell this far below entry
MIN_BID = float(os.getenv("EXIT_MIN_BID", "0.05"))       # don't dump into a near-dead book


def _f(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


async def _holdings(client: KalshiClient) -> Dict[str, dict]:
    """Reconstruct net yes holdings + avg entry from fills. {ticker: {qty, entry}}."""
    qty = defaultdict(float)
    cost = defaultdict(float)
    cursor = None
    for _ in range(10):
        r = await client._make_authenticated_request("GET", "/trade-api/v2/portfolio/fills",
                                                     params={"limit": 200, **({"cursor": cursor} if cursor else {})},
                                                     require_auth=True)
        fills = r.get("fills", []) or []
        for fl in fills:
            tk = fl.get("ticker") or fl.get("market_ticker")
            side = fl.get("side")
            if side != "yes" or not tk:
                continue
            c = _f(fl.get("count_fp")) or _f(fl.get("count")) or 0.0
            px = _f(fl.get("yes_price_dollars"))
            if px is None:
                continue
            if fl.get("action") == "buy":
                qty[tk] += c
                cost[tk] += c * px
            elif fl.get("action") == "sell":
                qty[tk] -= c
        cursor = r.get("cursor")
        if not cursor:
            break
    out = {}
    for tk, q in qty.items():
        if q > 0 and cost[tk] > 0:
            out[tk] = {"qty": q, "entry": cost[tk] / max(q, 1e-9)}
    return out


async def run_once(live: bool) -> list:
    client = KalshiClient()
    actions = []
    try:
        holds = await _holdings(client)
        for tk, h in holds.items():
            try:
                md = (await client.get_market(tk)).get("market", {})
            except Exception:
                continue
            bid = _f(md.get("yes_bid_dollars"))
            if bid is None or bid < MIN_BID:
                continue
            entry = h["entry"]
            # exit only on a genuine drop from where you bought (a favorite losing
            # its lead). Longshots that were always cheap are not "collapses".
            if bid <= entry - STOP_DROP:
                qty = int(h["qty"])
                if qty < 1:
                    continue
                actions.append((tk, entry, bid, qty))
                if live:
                    try:
                        await client.place_order(
                            ticker=tk, client_order_id=str(uuid.uuid4()), side="yes",
                            action="sell", count=qty, type_="limit",
                            yes_price=max(1, int(round(bid * 100))),  # hit the bid -> immediate fill
                        )
                        print(f"  SOLD {tk} x{qty} at {bid:.2f} (entry {entry:.2f}, cut loss).")
                    except Exception as e:
                        print(f"  sell failed {tk}: {e}")
    finally:
        await client.close()
    return actions


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
