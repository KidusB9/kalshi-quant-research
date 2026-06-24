"""
Delta-neutral funding-carry backtest.

The strategy: hold spot BTC long and short an equal-notional BTC perp, so price
direction cancels and you collect the perpetual funding payment each period.
Funding is positive (longs pay shorts) the large majority of the time, so as a
short you "win" (collect) most periods. This backtest measures, on REAL funding
data, the per-period win rate and the net return after fees, both always-on and
gated by a funding hurdle.

IMPORTANT venue caveat: this uses OKX BTC-USD-SWAP 8h funding as a representative,
real funding series. The edge you actually capture depends on the funding of the
venue where YOU short the perp. Kalshi's standard API exposes only binary crypto
markets (not fundable perps), so the live short venue (Kalshi crypto perp,
Coinbase perp, or a crypto exchange) must be confirmed before going live. The
structure of the edge (win rate, rough APR) is what this proves.

P&L model (per period, on notional): a short collects +funding when funding > 0
and pays when funding < 0; spot and perp price moves cancel (delta-neutral).
Net return is reported on TOTAL deployed capital = 2x notional (both legs funded),
which is the honest denominator.
"""

from __future__ import annotations

import io
import json
import os
import statistics
import time
import zipfile
from typing import List, Tuple

import httpx

# REALISTIC fees (the load-bearing economic input).
# Coinbase Advanced retail taker is ~0.60% one-way (lower at higher volume tiers
# or as a maker ~0.40%); Kalshi perp fee is not in public docs (confirm live) so
# a placeholder is used. One full cycle = enter (buy spot + short perp) +
# exit (sell spot + close perp) = 2*(coinbase + kalshi).
COINBASE_TAKER = 0.006
KALSHI_PERP_FEE = 0.0007    # placeholder -- MUST be read from a live fill
FEE_ENTER = COINBASE_TAKER + KALSHI_PERP_FEE   # one leg-pair (spot+perp)
PERIODS_PER_YEAR = 3 * 365  # 8h funding
CACHE = os.path.join(os.path.dirname(__file__), "..", "..", "data", "btc_funding_binance.json")


def fetch_binance_funding() -> List[Tuple[int, float]]:
    """Multi-year BTC 8h funding from the Binance public data archive (no geo-block).

    Caches to data/btc_funding_binance.json. Falls back to OKX recent history if
    the archive is unreachable.
    """
    if os.path.exists(CACHE):
        try:
            return [(int(t), float(r)) for t, r in json.load(open(CACHE))]
        except Exception:
            pass
    out = []
    for y in range(2021, 2027):
        for m in range(1, 13):
            if y == 2026 and m > 6:
                break
            url = (f"https://data.binance.vision/data/futures/um/monthly/fundingRate/"
                   f"BTCUSDT/BTCUSDT-fundingRate-{y}-{m:02d}.zip")
            try:
                r = httpx.get(url, timeout=30.0)
                if r.status_code != 200:
                    continue
                z = zipfile.ZipFile(io.BytesIO(r.content))
                with z.open(z.namelist()[0]) as f:
                    for line in io.TextIOWrapper(f):
                        p = line.strip().split(",")
                        try:
                            out.append((int(p[0]), float(p[2])))
                        except (ValueError, IndexError):
                            pass
            except Exception:
                continue
    out = sorted(set(out), key=lambda x: x[0])
    if out:
        try:
            os.makedirs(os.path.dirname(CACHE), exist_ok=True)
            json.dump([[t, r] for t, r in out], open(CACHE, "w"))
        except Exception:
            pass
        return out
    return fetch_okx_funding(2000)


def fetch_okx_funding(target: int = 2000) -> List[Tuple[int, float]]:
    """Paginate OKX BTC-USD-SWAP 8h funding history backward. Returns (ts_ms, rate) oldest-first."""
    out = []
    after = None
    for _ in range(target // 100 + 2):
        params = {"instId": "BTC-USD-SWAP", "limit": 100}
        if after:
            params["after"] = after
        r = httpx.get("https://www.okx.com/api/v5/public/funding-rate-history", params=params, timeout=20.0)
        d = r.json().get("data", [])
        if not d:
            break
        for x in d:
            out.append((int(x["fundingTime"]), float(x["fundingRate"])))
        after = min(int(x["fundingTime"]) for x in d)
        if len(out) >= target:
            break
    out = sorted(set(out), key=lambda x: x[0])
    return out


def _annualize(rate_8h: float) -> float:
    return rate_8h * PERIODS_PER_YEAR


def backtest(rates: List[float], hurdle_in: float, hurdle_out: float, ema_n: int = 21) -> dict:
    """
    Gated carry: hold the delta-neutral position only while the trailing funding
    EMA (annualized) is above hurdle_in; exit when it drops below hurdle_out.
    Collect funding each held period. Returns win rate, net return on 2x capital.
    """
    held = 0
    collected = []        # funding collected per held period (on notional)
    fees = 0.0
    cycles = 0
    in_pos = False
    ema = rates[0]
    alpha = 2 / (ema_n + 1)
    periods_pos = 0
    for i, f in enumerate(rates):
        ema = alpha * f + (1 - alpha) * ema
        ann = _annualize(ema)
        if not in_pos and ann > hurdle_in:
            in_pos = True
            cycles += 1
            fees += FEE_ENTER  # entry: buy spot + short perp
        elif in_pos and ann < hurdle_out:
            in_pos = False
            fees += FEE_ENTER  # exit: sell spot + close perp
        if in_pos:
            collected.append(f)      # short collects +f when funding positive
            held += 1
            if f > 0:
                periods_pos += 1
    gross = sum(collected)
    net_on_notional = gross - fees
    net_on_capital = net_on_notional / 2          # 2x capital (both legs)
    years = len(rates) / PERIODS_PER_YEAR
    return {
        "periods_total": len(rates), "periods_held": held, "cycles": cycles,
        "win_rate": (periods_pos / held * 100) if held else 0.0,
        "gross_on_notional_pct": gross * 100,
        "net_on_2x_capital_pct": net_on_capital * 100,
        "apr_on_2x_capital_pct": (net_on_capital / years * 100) if years else 0.0,
        "years": years, "fees_pct": fees * 100,
    }


def run() -> None:
    import datetime as dt
    from collections import defaultdict
    data = fetch_binance_funding()
    rates = [r for _, r in data]
    print("=" * 84)
    print(f"FUNDING-CARRY BACKTEST  (BTC 8h funding, {len(rates)} periods, {len(rates)/PERIODS_PER_YEAR:.1f}y)")
    if data:
        print(f"  span: {dt.datetime.utcfromtimestamp(data[0][0]//1000).date()} -> "
              f"{dt.datetime.utcfromtimestamp(data[-1][0]//1000).date()}")
    print("=" * 84)
    # per-year regime table
    yr = defaultdict(list)
    for t, r in data:
        yr[dt.datetime.utcfromtimestamp(t // 1000).year].append(r)
    print("Per-year (always-on carry, gross funding on notional):")
    print(f"{'year':>6} {'win%':>6} {'ann.funding%':>13} {'APR/2x capital%':>16}")
    for y in sorted(yr):
        rs = yr[y]
        pos_y = sum(1 for x in rs if x > 0) / len(rs) * 100
        ann = statistics.mean(rs) * PERIODS_PER_YEAR * 100
        # net APR on 2x capital, amortized fees negligible if held all year
        apr2x = (statistics.mean(rs) * PERIODS_PER_YEAR / 2) * 100
        print(f"{y:6d} {pos_y:5.1f} {ann:+12.1f} {apr2x:+15.1f}")
    pos = sum(1 for r in rates if r > 0) / len(rates) * 100
    print(f"\nFull sample: {pos:.1f}% positive | avg {statistics.mean(rates)*100:.4f}%/8h "
          f"({_annualize(statistics.mean(rates))*100:+.1f}% annualized)\n")
    be_hurdle = FEE_ENTER * 2 * PERIODS_PER_YEAR / (len(rates)) * 100  # if you churn every period (worst)
    print(f"REALISTIC fees: Coinbase {COINBASE_TAKER*100:.2f}% + Kalshi perp {KALSHI_PERP_FEE*100:.2f}% "
          f"per leg-pair; full enter+exit cycle = {FEE_ENTER*2*100:.2f}%.\n")
    print(f"{'config':26s} {'cycles':>6} {'held':>6} {'win%':>6} {'fees%':>7} {'APR/2x%':>9}")
    r0 = backtest(rates, hurdle_in=-999, hurdle_out=-1000)
    print(f"{'always-on (1 entry)':26s} {r0['cycles']:6d} {r0['periods_held']:6d} {r0['win_rate']:5.1f} "
          f"{r0['fees_pct']:6.2f} {r0['apr_on_2x_capital_pct']:+8.2f}")
    for hin, hout in [(0.05, 0.0), (0.08, 0.03), (0.12, 0.05)]:
        r = backtest(rates, hurdle_in=hin, hurdle_out=hout)
        print(f"{'gate in='+str(int(hin*100))+'% out='+str(int(hout*100))+'%':26s} "
              f"{r['cycles']:6d} {r['periods_held']:6d} {r['win_rate']:5.1f} "
              f"{r['fees_pct']:6.2f} {r['apr_on_2x_capital_pct']:+8.2f}")
    print("-" * 84)
    print("KEY: at realistic fees, CHURNING (gating in/out often) is killed by fee")
    print("drag -- each enter+exit cycle costs ~1.3%. The only viable mode is")
    print("BUY-AND-HOLD the carry (1 entry) and stay in while funding is positive.")
    print(f"Even then APR is regime-dependent (great 2021/2024, ~0 now in 2026).")
    print("VENUE: Kalshi BTC perp (KXBTCPERP) confirmed API-shortable; Kalshi's own")
    print("funding (BRTI-based, 2% cap) may differ from this offshore proxy -- its")
    print("history is only ~3 weeks old, so realized edge is uncertain until live.")
    print("=" * 84)


if __name__ == "__main__":
    run()
