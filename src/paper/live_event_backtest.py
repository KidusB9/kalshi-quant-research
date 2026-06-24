"""
Live-event convergence backtest.

Tests the speed/information edge: when a game (or any event) moves toward a
near-certain outcome, the market price climbs toward $1 but can lag the true
probability. Buying that climb and holding to settlement is profitable IF
markets that reach a high price resolve "yes" MORE often than the price implies
(net of the fee). If they resolve exactly at the implied rate, the market is
efficient and there is no edge.

This uses REAL data only: settled sports markets (known outcome) plus their full
intra-market price path from the Kalshi candlesticks endpoint. No external feed
is needed because the price path already encodes the game state (a 3-0 lead
shows up as the price jumping toward 1).

Methodology:
  For each settled market we walk its price path. The first time the market's
  yes mid crosses a confidence threshold T, we "buy yes" at the real yes_ask at
  that moment and hold to settlement. PnL per contract = (1 - entry) on a yes
  result, -entry on a no result, minus the taker fee. We also condition on
  timing (only entering in the late portion of the market's life) to isolate the
  "outcome is largely decided" case the strategy targets.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Tuple

from src.clients.kalshi_client import KalshiClient
from src.utils.fees import kalshi_taker_fee

CACHE = os.path.join(os.path.dirname(__file__), "..", "..", "data", "live_event_cache.json")
SPORT_KEYS = ("WC", "NFL", "NBA", "MLB", "NHL", "NCAA", "SOCCER", "UCL", "EPL",
              "TENNIS", "UFC", "GOLF", "PGA", "F1", "WNBA")


def _ts(iso: Optional[str]) -> Optional[int]:
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except (ValueError, AttributeError):
        return None


def _is_sport(tk: str) -> bool:
    t = tk.upper()
    if t.startswith("KXMVE"):
        return False
    return any(k in t for k in SPORT_KEYS)


def _cf(d, *keys) -> Optional[float]:
    """Pull close_dollars from a nested candle price block."""
    for k in keys:
        v = d.get(k)
        if isinstance(v, dict):
            c = v.get("close_dollars") or v.get("mean_dollars")
            if c is not None:
                try:
                    return float(c)
                except (TypeError, ValueError):
                    pass
    return None


@dataclass
class MarketPath:
    ticker: str
    result: str            # 'yes' / 'no'
    volume: float
    duration_h: float
    # path points: (frac_of_life 0..1, yes_bid, yes_ask)
    path: List[Tuple[float, float, float]] = field(default_factory=list)


async def collect(target: int = 300) -> List[MarketPath]:
    """Collect settled real-sports markets and their candlestick price paths."""
    if os.path.exists(CACHE):
        try:
            with open(CACHE) as f:
                raw = json.load(f)
            out = [MarketPath(**{**r, "path": [tuple(p) for p in r["path"]]}) for r in raw]
            if len(out) >= target * 0.5:
                print(f"loaded {len(out)} markets from cache")
                return out
        except Exception:
            pass

    client = KalshiClient()
    markets = []
    cursor = None
    try:
        # gather candidate settled sports markets
        cand = []
        for _ in range(40):
            try:
                r = await client.get_events(limit=200, cursor=cursor, status="settled", with_nested_markets=True)
            except Exception as e:
                print(f"events stop ({e})")
                break
            evs = r.get("events", []) or []
            if not evs:
                break
            for e in evs:
                for m in (e.get("markets") or []):
                    tk = m.get("ticker", "")
                    if not _is_sport(tk):
                        continue
                    res = (m.get("result") or "").lower()
                    if res not in ("yes", "no"):
                        continue
                    try:
                        v = float(m.get("volume_fp") or 0)
                    except (TypeError, ValueError):
                        v = 0
                    if v < 200:
                        continue
                    cand.append((tk, res, v, m.get("open_time"), m.get("close_time")))
            cursor = r.get("cursor")
            if not cursor or len(cand) >= target:
                break
        print(f"candidate settled sports markets: {len(cand)}")

        # pull candlestick path for each
        for tk, res, v, o, cl in cand[:target]:
            start, end = _ts(o), _ts(cl)
            if not start or not end or end <= start:
                continue
            dur_min = (end - start) / 60
            period = 1 if dur_min <= 4500 else (60 if dur_min <= 290000 else 1440)
            series = tk.split("-")[0]
            try:
                resp = await client._make_authenticated_request(
                    "GET", f"/trade-api/v2/series/{series}/markets/{tk}/candlesticks",
                    params={"period_interval": period, "start_ts": start, "end_ts": end},
                    require_auth=False,
                )
            except Exception:
                continue
            cs = resp.get("candlesticks", []) or []
            if len(cs) < 3:
                continue
            path = []
            for c in cs:
                t = c.get("end_period_ts")
                yb = _cf(c, "yes_bid")
                ya = _cf(c, "yes_ask")
                if t is None or yb is None or ya is None:
                    continue
                frac = (t - start) / (end - start)
                path.append((round(max(0, min(1, frac)), 4), yb, ya))
            if len(path) >= 3:
                markets.append(MarketPath(tk, res, v, round(dur_min / 60, 2), path))
    finally:
        await client.close()

    try:
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        with open(CACHE, "w") as f:
            json.dump([m.__dict__ for m in markets], f)
    except Exception:
        pass
    print(f"collected {len(markets)} markets with usable price paths")
    return markets


def backtest(markets: List[MarketPath], threshold: float, min_frac: float) -> dict:
    """
    Buy YES at the real ask the first time the yes mid crosses `threshold`,
    only if that happens at/after `min_frac` of the market's life. Hold to
    settlement. Returns aggregate stats.
    """
    n = wins = 0
    total = 0.0
    for m in markets:
        entry = None
        for frac, yb, ya in m.path:
            if frac < min_frac:
                continue
            mid = (yb + ya) / 2
            if mid >= threshold and 0.01 < ya < 0.999:
                entry = ya  # pay the ask to buy yes
                break
        if entry is None:
            continue
        won = (m.result == "yes")
        pnl = (1.0 - entry) if won else (-entry)
        pnl -= kalshi_taker_fee(entry, 1, "standard")
        n += 1
        wins += int(won)
        total += pnl
    return {
        "threshold": threshold, "min_frac": min_frac, "trades": n,
        "win_rate": (wins / n * 100) if n else 0.0,
        "avg_pnl": (total / n) if n else 0.0, "total": total,
    }


def run() -> None:
    markets = asyncio.run(collect(target=300))
    print("=" * 80)
    print("LIVE-EVENT CONVERGENCE BACKTEST  (real settled sports, real price paths)")
    print("=" * 80)
    if len(markets) < 30:
        print(f"Only {len(markets)} markets; sample too small for a verdict.")
        return
    short = [m for m in markets if m.duration_h <= 12]
    print(f"Markets: {len(markets)} total, {len(short)} short-lived (<=12h, game-like)")
    base = sum(1 for m in markets if m.result == "yes") / len(markets)
    print(f"Base yes-rate in sample: {base:.1%}\n")

    print("Rule: buy YES at the ask the first time the mid crosses T, in the late")
    print("portion of the market's life, hold to settlement. After real fees.\n")
    print(f"{'entry T':>8} {'late?':>10} {'trades':>7} {'win%':>6} {'avg $/ct':>9} {'total $':>9}")
    for min_frac, label in [(0.0, 'anytime'), (0.5, 'last 50%'), (0.75, 'last 25%')]:
        for T in (0.80, 0.85, 0.90, 0.95):
            r = backtest(markets, T, min_frac)
            if r["trades"] >= 10:
                tag = "  <- +EV" if r["avg_pnl"] > 0 else ""
                print(f"{T:8.2f} {label:>10} {r['trades']:7d} {r['win_rate']:5.1f} "
                      f"${r['avg_pnl']:+8.4f} ${r['total']:+8.2f}{tag}")
    print("\nGame-like markets only (<=12h):")
    for T in (0.85, 0.90, 0.95):
        r = backtest(short, T, 0.5)
        if r["trades"] >= 5:
            tag = "  <- +EV" if r["avg_pnl"] > 0 else ""
            print(f"  T={T:.2f} last50%: {r['trades']} trades, win {r['win_rate']:.1f}%, "
                  f"avg ${r['avg_pnl']:+.4f}/ct, total ${r['total']:+.2f}{tag}")
    print("=" * 80)


if __name__ == "__main__":
    run()
