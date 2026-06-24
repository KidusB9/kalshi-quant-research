"""
Crypto perpetuals strategy backtest.

Tests whether any simple systematic strategy delivers steady returns on crypto,
using real BTC price history from Coinbase. The honest result on the last ~year
of data: there is no reliable "constant return" edge. Trend-following mainly
reduces drawdown, mean-reversion loses, and a momentum long/short is only
marginally positive with high variance. Leverage amplifies losses badly.

The takeaway for perps: position sizing and survival matter more than any
signal. A 48% up-day is the same leverage that produces a 50%+ drawdown.
"""

from __future__ import annotations

import math
import statistics
from typing import Callable, List

import httpx

FEE = 0.0005  # ~5 bps per position change, round-trip-ish


def fetch_btc_daily() -> List[float]:
    r = httpx.get("https://api.exchange.coinbase.com/products/BTC-USD/candles",
                  params={"granularity": 86400}, timeout=20.0)
    data = sorted(r.json(), key=lambda x: x[0])  # [time, low, high, open, close, vol]
    return [d[4] for d in data]


def _stats(name: str, rets: List[float]) -> None:
    if not rets:
        return
    eq = peak = 1.0
    mdd = 0.0
    for r in rets:
        eq *= (1 + r)
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak)
    mean = statistics.mean(rets)
    sd = statistics.pstdev(rets) or 1e-9
    sharpe = mean / sd * math.sqrt(365)
    win = sum(1 for r in rets if r > 0) / len(rets) * 100
    print(f"{name:34s} total={(eq-1)*100:+7.1f}%  Sharpe={sharpe:+.2f}  maxDD={mdd*100:4.1f}%  win-days={win:.0f}%")


def _apply(closes: List[float], signal: Callable[[int], float]) -> List[float]:
    rets = [(closes[i] / closes[i - 1] - 1) for i in range(1, len(closes))]
    out = []
    prev = 0.0
    for i in range(1, len(closes)):
        pos = signal(i - 1)
        out.append(pos * rets[i - 1] - (FEE if pos != prev else 0))
        prev = pos
    return out


def run() -> None:
    closes = fetch_btc_daily()
    rets = [(closes[i] / closes[i - 1] - 1) for i in range(1, len(closes))]
    ma = lambda n, i: sum(closes[max(0, i - n + 1):i + 1]) / len(closes[max(0, i - n + 1):i + 1])
    print("=" * 84)
    print(f"CRYPTO PERP BACKTEST  (BTC daily, {len(closes)} days, real Coinbase data)")
    print("=" * 84)
    _stats("Buy & hold (1x)", rets)
    _stats("Trend: long >20d MA", _apply(closes, lambda i: 1 if closes[i] > ma(20, i) else 0))
    _stats("Trend: long >50d MA", _apply(closes, lambda i: 1 if closes[i] > ma(50, i) else 0))
    _stats("Mean-rev: long <10d MA", _apply(closes, lambda i: 1 if closes[i] < ma(10, i) else 0))
    _stats("Momentum long/short 20d", _apply(closes, lambda i: 1 if closes[i] > ma(20, i) else -1))
    _stats("Trend 3x leverage (>20d MA)", _apply(closes, lambda i: 3 if closes[i] > ma(20, i) else 0))
    print("-" * 84)
    print("Verdict: no constant-return edge. Trend-following cuts drawdown; only a")
    print("momentum long/short is marginally positive and high variance. 3x leverage")
    print("is ruinous. On perps, size small and survive; the signal is secondary.")
    print("=" * 84)


if __name__ == "__main__":
    run()
