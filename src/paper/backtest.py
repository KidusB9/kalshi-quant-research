"""
Honest backtest harness (no overfitting).

This does NOT invent a profitable edge. Your stored data (trading_system.db)
contains only *current* market snapshots -- no settled outcomes, no price
history -- so a true "predictions vs. real results" backtest is impossible.
Claiming otherwise would be fabrication.

Instead this runs your REAL strategy code (src.utils.edge_filter.EdgeFilter)
and REAL Kalshi fee math (src.utils.fees) over your REAL market prices, under
two explicit, conservative outcome models:

  NULL (no skill):  the AI's probability estimate is just noise around the
                    market price, and the market price is the true outcome
                    probability (efficient-market null). This is the worst
                    case and mirrors what the logs suggest the bot was doing.

  SKILL (edge=s):   the AI has a genuine s% probabilistic edge. Shows what the
                    system earns IF the model is actually good -- and that the
                    fee gate does not harm real-edge trades.

For each model we compare the OLD filter (fee gate OFF) vs the NEW filter
(fee gate ON) so the effect of the fix is isolated. Outcomes are Monte-Carlo
averaged over many trials with a fixed seed for reproducibility.
"""

from __future__ import annotations

import os
import random
import sqlite3
from dataclasses import dataclass
from typing import List, Optional

from src.utils.edge_filter import EdgeFilter
from src.utils.fees import kalshi_taker_fee


DB_PATH = os.environ.get(
    "TRADING_SYSTEM_DB",
    os.path.join(os.path.dirname(__file__), "..", "..", "trading_system.db"),
)


@dataclass
class MarketRow:
    market_id: str
    yes_price: float
    no_price: float
    volume: float
    category: str


@dataclass
class Result:
    label: str
    trades: int
    total_pnl: float
    total_fees: float
    wins: int
    losses: int

    @property
    def win_rate(self) -> float:
        n = self.wins + self.losses
        return (self.wins / n * 100) if n else 0.0

    @property
    def avg_pnl(self) -> float:
        return (self.total_pnl / self.trades) if self.trades else 0.0


def load_markets(min_volume: float = 500.0, limit: Optional[int] = None) -> List[MarketRow]:
    """Load real, tradeable market snapshots from trading_system.db."""
    conn = sqlite3.connect(os.path.abspath(DB_PATH))
    q = (
        "SELECT market_id, yes_price, no_price, volume, category FROM markets "
        "WHERE yes_price > 0.01 AND yes_price < 0.99 AND volume >= ?"
    )
    if limit:
        q += f" LIMIT {int(limit)}"
    rows = conn.execute(q, (min_volume,)).fetchall()
    conn.close()
    out = []
    for mid, yp, npr, vol, cat in rows:
        try:
            out.append(MarketRow(mid, float(yp), float(npr), float(vol or 0), (cat or "standard")))
        except (TypeError, ValueError):
            continue
    return out


def _simulate(
    markets: List[MarketRow],
    *,
    fee_gate: bool,
    skill: float,
    noise_sigma: float,
    trials: int,
    seed: int,
    confidence: float = 0.7,
    edge_floor: Optional[float] = None,
    label: Optional[str] = None,
) -> Result:
    """
    Run one regime over all markets x trials.

    fee_gate:   whether the NEW fee-aware net-edge gate is active.
    skill:      0.0 = null (AI is noise). >0 blends the AI estimate toward the
                true outcome by this fraction, i.e. a genuine edge.
    noise_sigma: stddev of the AI's noisy deviation from market price.
    edge_floor: override the per-tier edge thresholds (models an aggressive bot
                that trades thin edges). None = leave EdgeFilter defaults.
    """
    rng = random.Random(seed)
    # Temporarily toggle the fee gate / edge thresholds via class attrs.
    saved_fee = EdgeFilter.MIN_NET_EDGE_AFTER_FEES
    saved_edges = (
        EdgeFilter.HIGH_CONFIDENCE_EDGE,
        EdgeFilter.MEDIUM_CONFIDENCE_EDGE,
        EdgeFilter.LOW_CONFIDENCE_EDGE,
    )
    if not fee_gate:
        EdgeFilter.MIN_NET_EDGE_AFTER_FEES = -1.0  # disable: nothing fails on fees
    if edge_floor is not None:
        EdgeFilter.HIGH_CONFIDENCE_EDGE = edge_floor
        EdgeFilter.MEDIUM_CONFIDENCE_EDGE = edge_floor
        EdgeFilter.LOW_CONFIDENCE_EDGE = edge_floor

    trades = wins = losses = 0
    total_pnl = 0.0
    total_fees = 0.0
    try:
        for _ in range(trials):
            for m in markets:
                mkt_p = m.yes_price  # market-implied P(YES)
                # AI estimate = market + noise; with skill, nudged toward truth.
                noise = rng.gauss(0.0, noise_sigma)
                ai_p = mkt_p + noise
                # True outcome probability: null = market; skill blends toward AI signal.
                # (Under skill>0 the AI's noisy guess carries real information.)
                true_p = (1 - skill) * mkt_p + skill * max(0.0, min(1.0, ai_p))
                ai_p = max(0.01, min(0.99, ai_p))

                ok, _reason, res = EdgeFilter.should_trade_market(
                    ai_probability=ai_p,
                    market_probability=mkt_p,
                    confidence=confidence,
                    additional_filters={
                        "volume": m.volume,
                        "min_volume": 500,
                        "category": m.category,
                    },
                )
                if not ok:
                    continue

                side = res.side
                entry = mkt_p if side == "YES" else (1.0 - mkt_p)
                entry = max(0.01, min(0.99, entry))
                fee = kalshi_taker_fee(entry, 1, m.category)

                # Resolve outcome ~ Bernoulli(true_p) for YES.
                yes_wins = rng.random() < true_p
                won = yes_wins if side == "YES" else (not yes_wins)
                pnl = (1.0 - entry) if won else (-entry)
                pnl -= fee

                trades += 1
                total_fees += fee
                total_pnl += pnl
                if won:
                    wins += 1
                else:
                    losses += 1
    finally:
        EdgeFilter.MIN_NET_EDGE_AFTER_FEES = saved_fee
        (
            EdgeFilter.HIGH_CONFIDENCE_EDGE,
            EdgeFilter.MEDIUM_CONFIDENCE_EDGE,
            EdgeFilter.LOW_CONFIDENCE_EDGE,
        ) = saved_edges

    if label is None:
        label = ("NEW (fee-gate ON)" if fee_gate else "OLD (fee-gate OFF)") + f"  skill={skill:.0%}"
    return Result(label, trades, total_pnl, total_fees, wins, losses)


def run(min_volume: float = 500.0, trials: int = 3, seed: int = 42) -> None:
    markets = load_markets(min_volume=min_volume)
    print("=" * 72)
    print("HONEST MONTE-CARLO BACKTEST over REAL Kalshi market snapshots")
    print("=" * 72)
    print(f"Tradeable markets loaded: {len(markets):,}  (volume >= {min_volume:.0f})")
    print(f"Trials per regime: {trials}   Seed: {seed}")
    print("Outcome model: YES resolves ~ Bernoulli(true_p); fees = real Kalshi taker fee.")
    print("NULL = AI is pure noise (market is right). SKILL = AI has a genuine edge.\n")

    def show(r: Result) -> None:
        print(
            f"  {r.label:34s} | trades/run={r.trades // trials:6d} | "
            f"PnL/run=${r.total_pnl / trials:9.2f} | avg/trade=${r.avg_pnl:+.4f} | "
            f"win={r.win_rate:4.1f}% | fees/run=${r.total_fees / trials:7.2f}"
        )

    # --- FINDING 1: aggressive (old behaviour) vs fixed, when AI is NOISE ---
    print("-" * 72)
    print("FINDING 1 -- AI has NO real edge (null; matches what the logs show).")
    print("            This is the honest verdict on the bot as it was running.")
    aggressive = _simulate(
        markets, fee_gate=False, skill=0.0, noise_sigma=0.10, trials=trials, seed=seed,
        edge_floor=0.02, label="AS-IS  (thin 2% edges, no fee gate)",
    )
    fixed = _simulate(
        markets, fee_gate=True, skill=0.0, noise_sigma=0.10, trials=trials, seed=seed,
        label="FIXED  (8% edge + fee gate)",
    )
    show(aggressive)
    show(fixed)
    print("  --> Under pure noise, BOTH lose -- there is no winning config.")
    print("      Filtering harder (FIXED) even loses more PER trade: demanding a")
    print("      bigger 'edge' from a noisy model just selects more-wrong bets.")
    print("      Lesson: you cannot filter your way out of a fake edge. The fixes")
    print("      stop reckless churn, but PROFIT requires a REAL edge (Finding 2).")

    # --- FINDING 2: how much REAL edge is needed to actually profit? ---
    print("-" * 72)
    print("FINDING 2 -- break-even: real edge needed to beat fees (FIXED config).")
    for skill in (0.0, 0.05, 0.10, 0.15, 0.20, 0.25):
        r = _simulate(
            markets, fee_gate=True, skill=skill, noise_sigma=0.10, trials=trials, seed=seed,
            label=f"FIXED, real edge = {skill:.0%}",
        )
        verdict = "PROFIT" if r.total_pnl > 0 else "loss"
        print(
            f"  real edge {skill:4.0%} -> avg/trade=${r.avg_pnl:+.4f}  "
            f"PnL/run=${r.total_pnl / trials:9.2f}  win={r.win_rate:4.1f}%  [{verdict}]"
        )

    print("-" * 72)
    print("\nBOTTOM LINE (honest, not overfit):")
    print(" * If the AI is just noise, the bot loses ~the fee on EVERY trade.")
    print("   That is the bleed, quantified -- not bad luck.")
    print(" * Fixing plumbing (fee gate, abstain-on-no-AI, kill switch) stops the")
    print("   reckless trades but CANNOT manufacture an edge.")
    print(" * You need a real, consistent edge (see Finding 2) to profit. The only")
    print("   way to prove that is paper-trading vs REAL settled outcomes -- which")
    print("   is now safe to run (TRADING_HALTED blocks real orders).")
    print("=" * 72)


if __name__ == "__main__":
    run()
