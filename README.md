# Kalshi Quant Research

An autonomous quantitative trading system for Kalshi, the CFTC-regulated
event-contract exchange — built and operated the way a small trading firm is
structured: a fleet of execution engines up front, an evidence-gated risk layer
that governs their capital, and a machine-learning research layer that must earn
its way onto the book rather than assume it.

The guiding principle throughout: **no strategy touches real money until a
bootstrap confidence interval on its per-trade expected value clears zero on
real settled outcomes.** This document is an honest record of what was built,
what was tested, what was ruled out, and what survived.

> **Scope note.** This is a public engineering and research record. Live-edge
> strategy implementations and the current production engine fleet are kept in a
> private repository and are intentionally **not** published here.

---

## What it is

- **40+ async Python engines under one scheduler** — convergence trading,
  market-making quoters, sub-100 ms settlement-latency oracles, decided-state
  snipers, cross-family arbitrage, and a settlement-scored LLM analyst desk
  (Gemini → DeepSeek → OpenRouter failover, deliberately signal-only).
- **Defense in depth on real money** — per-engine daily-loss circuit breakers,
  a stop-loss / exit monitor, portfolio-concentration guards, and durable kill
  switches (including an email-driven remote halt), all of which fail closed.
- **An evidence-gated risk governor** at the core that audits every settled
  fill and rewrites live capital allocation automatically. Series and engines
  earn or lose their capital multiplier on their own realized, fills-true
  record — never on a backtest.

## Architecture

The system is organized in three layers that mirror a real desk.

### 1. The execution fleet (live)
A set of independent engines, each with a narrow, well-defined edge hypothesis,
a hard capital cap, and its own daily-loss breaker. Engines run as isolated
subprocesses under a single scheduler with a cross-process single-instance lock,
so the fleet survives crashes and restarts without double-trading. A WebSocket
book feed and an event-driven watcher let the latency-sensitive engines react to
live order-book dislocations in seconds rather than polling minutes.

### 2. The evidence-gated risk governor
The heart of the system. Every few hours it re-audits realized results per
engine and per market series (joined fills-true against settlement), then:
- benches any series that is losing on a real sample, with an evidence-gated
  path back — it must prove itself in a shadow book before capital returns;
- steps each engine/series capital multiplier up or down within hard bounds;
- enforces an absolute per-tick spend ceiling that no multiplier can breach.

Every decision is logged with its reasoning. The rule that governs the rule is
the confidence interval: capital follows demonstrated, settled expected value.

### 3. The ML research layer (shadow)
Structured the way a research desk sits beside a trading desk — and, by design,
**it does not drive live orders until it earns a seat:**

| Component | Role |
|---|---|
| **Data lake + training** | A 3.7 GB settled-outcome data lake (5.5M+ settled markets, updated daily with targeted backfill). Walk-forward training pipelines are gated on **beating the market's own out-of-sample Brier score** — a model that cannot out-predict the closing price does not ship. |
| **Pricing service** | Re-prices the tradeable venue against the current model every 15 minutes and logs net-of-fee edges. |
| **Model + analyst desks** | A shadow book: an ML "model desk" and an LLM "analyst desk" log predictions that are then **settlement-scored against real outcomes.** To date: **591 settlement-scored shadow trades** and **1,447 scored LLM forecasts**, across **14 versioned model generations** in a registry carrying honest per-category Brier scores. |

The ML brain runs its own book and is deliberately walled off from live order
placement. It must pass the same statistical gates the live engines passed. At
current sample sizes it has **not** earned a live seat — and the system says so
on its own scoreboard rather than pretending otherwise.

### Independent intelligence & risk layers
Wrapping the desks, running continuously and independently of any single engine:

- **Portfolio guard** — watches the real open positions the live engines create
  and flags concentration breaches against configured caps.
- **Opportunity book** — a machine-evaluated register of every known
  opportunity; alerts the moment a trigger flips and changes what the desk
  should be doing.
- **Venue census** — diffs the entire exchange daily to catch new listings,
  fee-type changes, and regime shifts in the traded universes.

## Research record — what was tested, what survived

The value of this project is the honest record, not a highlight reel.

**Survived:** a late-life **convergence edge** on event contracts — near-certain
outcomes that the market prices toward but lags — validated on hundreds of real
settled markets using full intra-market price paths from the exchange's
candlestick history, with fills confirmed against live order books. The margins
are real but thin, the edge lives in slow multi-day markets and dies in fast
in-play ones, and the live fleet is gated accordingly.

**Ruled out, each with a measured reason** (a representative sample):
- **LLM directional prediction** — asking models to price events and bet the gap
  is noise; the market already reflects the same public information, and a
  no-skill Monte Carlo shows you need an unrealistic edge just to clear fees.
- **Complete-set arbitrage** — mutually-exclusive markets summing below $1 look
  free but are not: the gap is priced tail risk from an unlisted outcome.
- **Weather-settlement latency** — a genuine, verified pricing edge, but
  unreachable: free data feeds arrive after the book reprices, and the
  profitable rungs hold only 1–2 contracts. Real edge, no capacity.
- **Crypto settlement oracle** — a warm multi-venue feed genuinely ahead of the
  settlement print (46–122 ms), but the determined side already asks $1.00: the
  book is efficient exactly where it is liquid.
- **A maker-strategy methodology lesson** — tape-replay backtests systematically
  over-credit passive fills because they cannot model adverse selection; the
  system now trusts only live fills or taker-on-tape evidence for maker ideas.

The through-line: an edge is only real when it survives fees, depth, adverse
selection, and a confidence interval — not when it looks good on a chart.

## Engineering

- **~58k lines of Python, 1,400+ tests.**
- Async scheduler with subprocess isolation, single-instance locking, and
  automatic restart/recovery of both the fleet and its data-feed daemons.
- Cryptographically signed (RSA-PSS) order placement against the live API, with
  kill switches enforced at the universal order chokepoint so no launch path can
  bypass them.
- A resumable data pipeline maintaining a multi-gigabyte settled-outcome lake.

## Results & honest limitations

This is an **engineering and research showcase**, and it is presented as one.
Operated at small personal capital, net realized trading P&L is roughly
**break-even** — by design. The objective was the risk architecture, the
evidence discipline, and the research machinery, not returns on a few hundred
dollars: the binding constraint on this venue is *opportunity and book depth*,
not capital or code. The same architecture is what would let proven edges scale
if and when capital and opportunity allow. Nothing here claims a profit it did
not make.

## Attribution

Built on the open-source scaffold
[ryanfrigo/kalshi-ai-trading-bot](https://github.com/ryanfrigo/kalshi-ai-trading-bot).
The engine fleet, the quant/risk/ML machinery, and the research record are
original work.

## Disclaimer

Trading carries real risk of loss. Nothing here is financial advice and there is
no guarantee of profit. Backtested results are not a promise of future
performance.
