"""
Kalshi funding-carry monitor and go/no-go gate.

The funding carry is only worth running when the SHORT leg's venue (Kalshi
KXBTCPERP) pays positive funding often enough and large enough to clear costs.
Offshore funding (Binance/OKX) is positive ~86% of the time, but Kalshi's own
perp funding is premium-driven, US-retail, and behaves differently. This tool
reads Kalshi's REAL funding (live estimate + recent history), nets it against
Binance.US spot fees, and gives an honest go/no-go.

It also appends each reading to a log so the (currently ~3-week-old) Kalshi
funding series accumulates until there is enough to trust.

Read-only. Places no orders.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import time
from datetime import datetime, timezone
from typing import List

from src.clients.kalshi_client import KalshiClient

PERPS_HOST = "https://external-api.kalshi.com"
PERIODS_PER_YEAR = 3 * 365
BINANCE_MAKER_FEE = 0.0000      # Binance.US maker is currently 0%
BINANCE_TAKER_FEE = 0.0002
LOG = os.path.join(os.path.dirname(__file__), "..", "..", "data", "kalshi_funding_log.json")

# Deploy only if funding is positive clearly more often than not AND the net
# annualized carry beats a buffer over costs.
MIN_POS_FRACTION = 0.65         # short collects in >=65% of periods
MIN_NET_APR = 0.05             # >=5% net annualized after fees


async def _req(c: KalshiClient, path: str, params=None):
    return await c._make_authenticated_request("GET", path, params=params, require_auth=True)


async def read_funding() -> dict:
    c = KalshiClient()
    c.base_url = PERPS_HOST
    try:
        enabled = (await _req(c, "/trade-api/v2/margin/enabled")).get("enabled")
        est = await _req(c, "/trade-api/v2/margin/funding_rates/estimate", {"ticker": "KXBTCPERP"})
        now = int(time.time())
        hist = await _req(c, "/trade-api/v2/margin/funding_rates/historical",
                          {"ticker": "KXBTCPERP", "start_ts": now - 30 * 86400, "end_ts": now})
        rates = [float(x["funding_rate"]) for x in hist.get("funding_rates", [])]
        return {"enabled": enabled, "live_rate_8h": float(est["funding_rate"]),
                "next_funding_time": est.get("next_funding_time"), "hist_rates": rates}
    finally:
        await c.close()


def _append_log(live_rate_8h: float) -> None:
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "rate_8h": live_rate_8h}
    data = []
    if os.path.exists(LOG):
        try:
            data = json.load(open(LOG))
        except Exception:
            data = []
    data.append(rec)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    json.dump(data, open(LOG, "w"), indent=2)


def assess(rates: List[float]) -> dict:
    if not rates:
        return {"n": 0}
    pos_frac = sum(1 for r in rates if r > 0) / len(rates)
    avg_ann = statistics.mean(rates) * PERIODS_PER_YEAR
    # net carry on notional: collected funding minus a tiny per-period spot maker
    # fee (assume hold long, near-zero churn). Short collects +rate when positive.
    net_ann = avg_ann - BINANCE_MAKER_FEE * PERIODS_PER_YEAR  # maker ~0 -> net ~= avg
    return {
        "n": len(rates), "pos_fraction": pos_frac, "avg_apr": avg_ann,
        "net_apr": net_ann, "min_apr": min(rates) * PERIODS_PER_YEAR,
        "max_apr": max(rates) * PERIODS_PER_YEAR,
    }


def run() -> None:
    data = asyncio.run(read_funding())
    _append_log(data["live_rate_8h"])
    a = assess(data["hist_rates"])
    print("=" * 78)
    print("KALSHI FUNDING-CARRY MONITOR  (KXBTCPERP short leg)")
    print("=" * 78)
    print(f"margin trading enabled on account: {data['enabled']}  "
          f"{'(OK)' if data['enabled'] else '(BLOCKER -- enable margin on Kalshi to trade perps)'}")
    print(f"live funding: {data['live_rate_8h']*100:.4f}%/8h "
          f"({data['live_rate_8h']*PERIODS_PER_YEAR*100:+.1f}% annualized) | next {data['next_funding_time']}")
    if a["n"]:
        print(f"\nrecent Kalshi funding ({a['n']} periods, ~{a['n']/3:.0f} days):")
        print(f"  positive (short collects): {a['pos_fraction']*100:.0f}% of periods")
        print(f"  avg {a['avg_apr']*100:+.1f}% annualized | range {a['min_apr']*100:+.0f}% to {a['max_apr']*100:+.0f}%")
        print(f"  net after Binance.US fees: {a['net_apr']*100:+.1f}% annualized")
        favorable = (a["pos_fraction"] >= MIN_POS_FRACTION and a["net_apr"] >= MIN_NET_APR and data["enabled"])
        print("\nGO/NO-GO:")
        if favorable:
            print(f"  GO -- funding positive {a['pos_fraction']*100:.0f}% (need >={MIN_POS_FRACTION*100:.0f}%), "
                  f"net {a['net_apr']*100:+.1f}% APR (need >={MIN_NET_APR*100:.0f}%). Carry is worth running.")
        else:
            reasons = []
            if not data["enabled"]:
                reasons.append("margin not enabled")
            if a["pos_fraction"] < MIN_POS_FRACTION:
                reasons.append(f"funding positive only {a['pos_fraction']*100:.0f}% (need >={MIN_POS_FRACTION*100:.0f}%)")
            if a["net_apr"] < MIN_NET_APR:
                reasons.append(f"net APR {a['net_apr']*100:+.1f}% below {MIN_NET_APR*100:.0f}%")
            print(f"  NO-GO -- {'; '.join(reasons)}.")
            print("  Do NOT deploy the carry. The short would PAY funding most periods.")
    print("\nNote: Kalshi perp history is only ~3 weeks old. Run this daily; the log")
    print("at data/kalshi_funding_log.json accumulates the series we need to trust a GO.")
    print("=" * 78)


if __name__ == "__main__":
    run()
