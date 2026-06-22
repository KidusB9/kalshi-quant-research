"""
Settled-market outcome backtest -- REAL outcomes, REAL prices.

Pulls resolved Kalshi markets from the live API (each carries a ``result`` of
'yes'/'no' and a ``last_price_dollars`` = last traded price before settlement)
and measures CALIBRATION: does the realized YES rate match the implied price?

A systematic gap is the favorite-longshot bias -- the one edge the research
flagged as plausibly real. We then compute the after-fee EV of concrete
fade/back rules using the realized rates.

HONEST LIMITS (stated up front, not buried):
  * last_price is the price near close, so it already absorbs late information.
    This UNDERSTATES any exploitable edge (earlier prices would show more). If
    even the near-close price is miscalibrated, the signal is strong.
  * No order-book depth: EV assumes you fill 1 contract at last_price + taker
    fee. Real fills face spread/slippage; treat EV as an upper bound.
  * Survivorship/selection: we sample whatever the API returns, filtered to
    real liquidity (volume >= min_volume) and a tradeable price (1c-99c).
"""

from __future__ import annotations

import asyncio
import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from src.clients.kalshi_client import KalshiClient
from src.utils.fees import kalshi_taker_fee


@dataclass
class Mkt:
    ticker: str
    implied: float   # last traded price (YES), 0-1
    yes_won: bool
    volume: float
    series: str


def _f(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


async def pull_settled(target: int = 12000, min_volume: float = 100.0) -> List[Mkt]:
    """Paginate settled markets from the live API into a clean sample."""
    client = KalshiClient()
    out: List[Mkt] = []
    seen = 0
    cursor = None
    try:
        while seen < target:
            resp = await client.get_markets(limit=200, cursor=cursor, status="settled")
            mkts = resp.get("markets", []) or []
            if not mkts:
                break
            for m in mkts:
                seen += 1
                res = (m.get("result") or "").lower()
                if res not in ("yes", "no"):
                    continue
                price = _f(m.get("last_price_dollars"))
                if price is None:
                    lp = _f(m.get("last_price"))
                    price = lp / 100.0 if lp is not None else None
                vol = _f(m.get("volume_fp")) or _f(m.get("volume")) or 0.0
                if price is None or not (0.01 <= price <= 0.99) or vol < min_volume:
                    continue
                ticker = m.get("ticker", "?")
                series = ticker.split("-", 1)[0]
                out.append(Mkt(ticker, price, res == "yes", vol, series))
            cursor = resp.get("cursor")
            if not cursor:
                break
    finally:
        await client.close()
    return out


def _wilson_halfwidth(p: float, n: int, z: float = 1.96) -> float:
    """Approx half-width of a Wilson-ish CI for a proportion (for sanity bands)."""
    if n == 0:
        return 1.0
    return z * math.sqrt(max(p * (1 - p), 1e-9) / n)


def calibration_table(sample: List[Mkt], bands: int = 10) -> List[Tuple]:
    """Bucket by implied price; return (lo, hi, n, mean_implied, realized, bias, ci)."""
    buckets: Dict[int, List[Mkt]] = defaultdict(list)
    for m in sample:
        idx = min(bands - 1, int(m.implied * bands))
        buckets[idx].append(m)
    rows = []
    for idx in range(bands):
        g = buckets.get(idx, [])
        if not g:
            continue
        n = len(g)
        mean_implied = sum(x.implied for x in g) / n
        realized = sum(1 for x in g if x.yes_won) / n
        bias = realized - mean_implied
        ci = _wilson_halfwidth(realized, n)
        rows.append((idx / bands, (idx + 1) / bands, n, mean_implied, realized, bias, ci))
    return rows


def strategy_ev(sample: List[Mkt], *, side: str, lo: float, hi: float) -> Tuple[int, float, float, float]:
    """
    After-fee EV per contract for buying `side` on markets whose YES price is in
    [lo, hi). Returns (n_trades, avg_ev, win_rate, total_ev).
    """
    n = 0
    total = 0.0
    wins = 0
    for m in sample:
        if not (lo <= m.implied < hi):
            continue
        if side == "YES":
            entry = m.implied
            won = m.yes_won
        else:  # NO
            entry = 1.0 - m.implied
            won = not m.yes_won
        if not (0.01 <= entry <= 0.99):
            continue
        fee = kalshi_taker_fee(entry, 1, "standard")
        pnl = (1.0 - entry) if won else (-entry)
        pnl -= fee
        n += 1
        total += pnl
        if won:
            wins += 1
    avg = total / n if n else 0.0
    wr = wins / n * 100 if n else 0.0
    return n, avg, wr, total


def run(min_volume: float = 100.0, target: int = 12000) -> None:
    print("=" * 78)
    print("SETTLED-MARKET OUTCOME BACKTEST  (real Kalshi results, real prices)")
    print("=" * 78)
    sample = asyncio.run(pull_settled(target=target, min_volume=min_volume))
    print(f"Resolved markets in clean sample: {len(sample):,}  (volume >= {min_volume:.0f}, price 1-99c)")
    if len(sample) < 50:
        print("Sample too small to conclude. Try lowering min_volume.")
        return
    base_yes = sum(1 for m in sample if m.yes_won) / len(sample)
    print(f"Overall YES-resolution rate: {base_yes:.1%}\n")

    print("CALIBRATION  (does realized match the price you'd pay?)")
    print(f"{'price band':>12} | {'n':>5} | {'implied':>7} | {'realized':>8} | {'bias':>7} | 95% CI")
    print("-" * 78)
    rows = calibration_table(sample, bands=10)
    for lo, hi, n, imp, real, bias, ci in rows:
        flag = ""
        if abs(bias) > ci and abs(bias) > 0.02:
            flag = "  <-- miscalibrated" if bias > 0 else "  <-- overpriced YES"
        print(f"{lo:5.2f}-{hi:4.2f} | {n:5d} | {imp:7.1%} | {real:8.1%} | {bias:+6.1%} | +/-{ci:4.1%}{flag}")

    print("\nAFTER-FEE EV of concrete rules (1 contract, fill at last price + taker fee):")
    print(f"{'rule':46s} | {'n':>5} | {'win%':>5} | {'avg $/trade':>11} | total $")
    print("-" * 78)
    rules = [
        ("Back FAVORITES: buy YES where price 0.85-0.99", "YES", 0.85, 0.99),
        ("Back favorites lite: buy YES where 0.70-0.85", "YES", 0.70, 0.85),
        ("Fade LONGSHOTS: buy NO where YES price 0.01-0.15", "NO", 0.01, 0.15),
        ("Fade longshots lite: buy NO where YES 0.15-0.30", "NO", 0.15, 0.30),
        ("Coin-flip zone: buy YES where 0.45-0.55", "YES", 0.45, 0.55),
    ]
    best = None
    for label, side, lo, hi in rules:
        n, avg, wr, total = strategy_ev(sample, side=side, lo=lo, hi=hi)
        tag = "  PROFIT" if avg > 0 else ""
        print(f"{label:46s} | {n:5d} | {wr:4.1f} | ${avg:+10.4f} | ${total:+8.2f}{tag}")
        if n >= 50 and (best is None or avg > best[1]):
            best = (label, avg, n, total)

    # Slippage haircut: you fill at the ASK, not last_price. Assume ~2c worse
    # entry, and require the edge to survive that before calling it a candidate.
    SLIPPAGE = 0.02
    print("-" * 78)
    if best and best[1] - SLIPPAGE > 0 and best[2] >= 100:
        print(f"CANDIDATE rule (survives a {SLIPPAGE*100:.0f}c slippage haircut): {best[0]}")
        print(f"  gross after-fee +${best[1]:.4f}/contract over {best[2]} trades;")
        print(f"  ~+${best[1]-SLIPPAGE:.4f}/contract after a {SLIPPAGE*100:.0f}c slippage haircut.")
    elif best and best[1] > 0:
        print(f"WEAK signal: {best[0]}")
        print(f"  +${best[1]:.4f}/contract gross, but this does NOT survive a {SLIPPAGE*100:.0f}c")
        print(f"  slippage haircut and/or the sample (n={best[2]}) is too thin to trust.")
    else:
        print("No rule is positive after fees. Honest negative -- no money risked.")
    print("\nDO NOT TRADE THIS YET. Why this is a screen, not a green light:")
    print(" * Sample is dominated by longshot legs of multi-outcome fields (low YES")
    print("   base rate), so 'buy NO' wins a lot mechanically -- the edge is only the")
    print("   thin gap between implied price and realized rate, not the high win-%.")
    print(" * 0 observed hits in the cheapest band means tail risk is UNMEASURED; one")
    print("   longshot hitting costs ~$0.97 and erases dozens of 3c wins.")
    print(" * last_price is near-close and can be stale on thin books; real fills pay")
    print("   the ask + spread. The only honest confirmation is FORWARD-testing this")
    print("   rule in paper mode vs live settlements (TRADING_HALTED stays on).")
    print("=" * 78)


if __name__ == "__main__":
    run()
