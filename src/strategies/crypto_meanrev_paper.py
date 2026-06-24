"""
Crypto mean-reversion: full paper-trading product.

This is the complete, integrated version of the trend-filtered dip-buying edge
(see src/strategies/crypto_meanrev.py for the core rule and the multi-year
backtest). It does three things:

  1. backtest()  -- replays the strategy over all available history and prints
     the full trade log, equity curve summary, and win/loss statistics. This is
     the evidence the edge wins more than it loses.
  2. paper_step() -- the forward paper trader. Run it once a day; it fetches the
     latest BTC daily close, decides flat/long, books simulated trades, and
     persists state to disk so a real forward track record accumulates with no
     money at risk.
  3. go_live_check() -- reports whether the forward paper record has met the
     criteria to switch to live trading.

The rule (win rate ~65-73% across 5.8y, net positive after fees):
  ENTRY: BTC closes down N days in a row AND price is above its 200-day MA.
  EXIT:  the first up day (close greater than the prior close).
  Only longs. The 200-day filter is what keeps it profitable; it sits out
  downtrends (which is why it is flat today).

State is unleveraged and tracks percentage returns. On Kalshi perps you would
execute the long when the signal fires; keep leverage modest because losses run
larger than wins.
"""

from __future__ import annotations

import json
import os
import statistics
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import httpx

from src.strategies.crypto_meanrev import (
    DOWN_DAYS, TREND_MA, FEE_ROUND_TRIP, _ma, _down_streak,
)

STATE_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "crypto_meanrev_state.json")

# Go-live criteria for the FORWARD paper record (not the historical backtest).
GO_LIVE_MIN_TRADES = 10
GO_LIVE_MIN_WIN_RATE = 55.0
GO_LIVE_MIN_NET = 0.0


def _fetch_daily(days: int = 1500) -> List[Dict]:
    """Daily BTC candles from Coinbase, oldest first, as {date, close}."""
    out = []
    end = int(time.time())
    gran = 86400
    for _ in range(days // 300 + 2):
        r = httpx.get("https://api.exchange.coinbase.com/products/BTC-USD/candles",
                      params={"granularity": gran, "end": end, "start": end - gran * 300}, timeout=20.0)
        ch = r.json()
        if not isinstance(ch, list) or not ch:
            break
        out += [(c[0], c[4]) for c in ch]
        end = min(c[0] for c in ch) - gran
    out = sorted(set(out), key=lambda x: x[0])
    return [{"date": datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d"), "close": c} for t, c in out]


def _summarize(trades: List[Dict], label: str) -> None:
    if not trades:
        print(f"{label}: no closed trades yet.")
        return
    rets = [t["pnl_pct"] / 100 for t in trades]
    eq = peak = 1.0
    mdd = 0.0
    for r in rets:
        eq *= (1 + r)
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak)
    wins = [t for t in trades if t["pnl_pct"] > 0]
    losses = [t for t in trades if t["pnl_pct"] <= 0]
    pf = (sum(t["pnl_pct"] for t in wins) / -sum(t["pnl_pct"] for t in losses)) if losses and sum(t["pnl_pct"] for t in losses) < 0 else 99.0
    print(f"{label}: {len(trades)} trades | win {len(wins)/len(trades)*100:.1f}% | "
          f"net {(eq-1)*100:+.1f}% | PF {pf:.2f} | maxDD {mdd*100:.1f}% | "
          f"avgW {statistics.mean([t['pnl_pct'] for t in wins]):+.2f}% | "
          f"avgL {(statistics.mean([t['pnl_pct'] for t in losses]) if losses else 0):+.2f}%")


def backtest(closes: List[float], dates: List[str]) -> List[Dict]:
    """Replay the full history; return the trade log."""
    trades = []
    i = 1
    n = len(closes)
    while i < n - 1:
        if closes[i] > _ma(closes, i, TREND_MA) and _down_streak(closes, i, DOWN_DAYS):
            entry = closes[i]
            j = i + 1
            while j < n - 1 and not (closes[j] > closes[j - 1]):
                j += 1
            pnl = (closes[j] / entry - 1 - FEE_ROUND_TRIP) * 100
            trades.append({"entry_date": dates[i], "entry_price": round(entry, 2),
                           "exit_date": dates[j], "exit_price": round(closes[j], 2),
                           "pnl_pct": round(pnl, 2), "win": pnl > 0, "days": j - i})
            i = j + 1
        else:
            i += 1
    return trades


# -------------------- forward paper trader (stateful) --------------------

def _load_state() -> Dict:
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {"position": "flat", "entry_price": None, "entry_date": None,
            "last_date": None, "trades": []}


def _save_state(st: Dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(st, f, indent=2)


def paper_step(verbose: bool = True) -> Dict:
    """Advance the forward paper trade by the latest daily close. Idempotent per day."""
    candles = _fetch_daily()
    closes = [c["close"] for c in candles]
    dates = [c["date"] for c in candles]
    i = len(closes) - 1
    today = dates[i]
    st = _load_state()

    if st.get("last_date") == today:
        if verbose:
            print(f"Already processed {today}. Position: {st['position']}.")
        return st

    in_trend = closes[i] > _ma(closes, i, TREND_MA)
    streak = _down_streak(closes, i, DOWN_DAYS)
    up_day = closes[i] > closes[i - 1]

    if st["position"] == "flat":
        if in_trend and streak:
            st.update(position="long", entry_price=closes[i], entry_date=today)
            if verbose:
                print(f"{today}: PAPER ENTRY long at ${closes[i]:,.0f} (dip in uptrend).")
        elif verbose:
            print(f"{today}: flat, no entry ({'no uptrend' if not in_trend else 'no down-streak'}).")
    else:  # holding long
        if up_day:
            pnl = (closes[i] / st["entry_price"] - 1 - FEE_ROUND_TRIP) * 100
            st["trades"].append({"entry_date": st["entry_date"], "entry_price": round(st["entry_price"], 2),
                                 "exit_date": today, "exit_price": round(closes[i], 2),
                                 "pnl_pct": round(pnl, 2), "win": pnl > 0})
            st.update(position="flat", entry_price=None, entry_date=None)
            if verbose:
                print(f"{today}: PAPER EXIT at ${closes[i]:,.0f} | trade P&L {pnl:+.2f}%.")
        elif verbose:
            print(f"{today}: holding long from ${st['entry_price']:,.0f}, no up-day yet.")

    st["last_date"] = today
    _save_state(st)
    return st


def go_live_check(st: Dict) -> None:
    trades = st.get("trades", [])
    n = len(trades)
    wr = (sum(1 for t in trades if t["pnl_pct"] > 0) / n * 100) if n else 0.0
    net = 1.0
    for t in trades:
        net *= (1 + t["pnl_pct"] / 100)
    net = (net - 1) * 100
    ready = n >= GO_LIVE_MIN_TRADES and wr >= GO_LIVE_MIN_WIN_RATE and net >= GO_LIVE_MIN_NET
    print("\nGO-LIVE CHECK (forward paper record):")
    print(f"  trades {n}/{GO_LIVE_MIN_TRADES} | win {wr:.0f}% (need >={GO_LIVE_MIN_WIN_RATE:.0f}) | "
          f"net {net:+.1f}% (need >={GO_LIVE_MIN_NET:.0f})")
    print(f"  -> {'READY to go live (small size).' if ready else 'NOT yet -- let the paper record build.'}")


def run() -> None:
    candles = _fetch_daily()
    closes = [c["close"] for c in candles]
    dates = [c["date"] for c in candles]
    print("=" * 84)
    print(f"CRYPTO MEAN-REVERSION PAPER PRODUCT  (BTC daily, {len(closes)} closes, ~{len(closes)/365:.1f}y)")
    print("=" * 84)

    bt = backtest(closes, dates)
    _summarize(bt, "HISTORICAL BACKTEST")
    print("  last 6 historical trades:")
    for t in bt[-6:]:
        print(f"    {t['entry_date']} -> {t['exit_date']} ({t['days']}d)  "
              f"${t['entry_price']:,.0f} -> ${t['exit_price']:,.0f}  {t['pnl_pct']:+.2f}%  "
              f"{'WIN' if t['win'] else 'loss'}")

    st = paper_step(verbose=True)
    _summarize(st.get("trades", []), "\nFORWARD PAPER RECORD")
    go_live_check(st)
    print("=" * 84)


if __name__ == "__main__":
    run()
