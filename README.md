# Kalshi Quant Research

A research and execution framework for systematic trading on Kalshi, a
CFTC-regulated event-contract exchange. The project pairs a full backtesting
suite that runs against real settled outcomes with live execution modules and
hard risk controls. The guiding principle throughout is simple: no strategy goes
near real money until it is backtested on real data and the edge survives real
fills and fees.

This document is an honest record of what was tested, what was ruled out, and
what survived. The surviving edge is event convergence. Crypto-perpetual
research is ongoing.

## Background: pricing and fees

A contract trades between $0.01 and $0.99 and settles at $1.00 if the event
resolves yes, so the price is the market's implied probability. Kalshi charges a
per-contract fee of `ceil(0.07 * P * (1 - P) * 100)` cents on standard markets
(`0.035` on index markets), with maker orders charged a quarter of the taker
rate. On a binary contract the probability edge equals the per-contract dollar
edge: buying yes at price p with true probability q has expected value `q - p`.
That identity makes the break-even math exact and is the backbone of every test
below.

## Validated edge: event convergence

When an event moves toward a near-certain outcome, the market price climbs toward
$1 but tends to lag the true probability. Buying that climb late in the market's
life, while there is still real margin in the ask, and holding to settlement is
positive expected value.

This was backtested on 300 real settled markets using their full intra-market
price paths from the Kalshi candlesticks endpoint, so the price path itself
encodes the state of the underlying event. Buying yes at the real ask the first
time the mid crosses a high-confidence threshold in the late portion of the
market's life, then holding to settlement, returned roughly +0.04 to +0.065 per
contract after fees with a 97 to 100 percent win rate across thresholds. The
asks were confirmed fillable against live order books.

Honest constraints: the margin per contract is a few cents, a rare reversal
loses most of the stake, and the backtest sample contains correlated outcomes,
so the strategy should be run at small size and the live win rate confirmed
before scaling.

Implementation: `src/strategies/convergence_trader.py`. It enters only when the
mid is high, the ask still leaves margin after fees, the market is late in its
life, the ask is fillable, and no correlated position is already open, all within
hard capital caps. Backtest harness: `src/paper/live_event_backtest.py`.

## What was tested and ruled out

- **LLM directional prediction.** Asking models to price events and bet the gap
  versus the market. No edge: the market already reflects the same public
  information, so the model's signal is noise. A no-skill Monte Carlo showed you
  need an unrealistic 18 to 20 point edge just to clear fees.
- **Complete-set arbitrage.** Groups of mutually exclusive markets summing below
  $1 look like free money but are not: mutually exclusive does not mean
  exhaustive, so the gap is priced tail risk from an unlisted outcome.
- **Favorite-longshot fade.** Cheap longshots are genuinely overpriced, but the
  no-side ask sits well above fair value, so the spread eats the edge.
- **Plain market making.** Wide gross spreads (about 4 cents median) but adverse
  selection drags spread capture to roughly break-even on its own.

## Other modules

- **Liquidity provision.** `src/strategies/liquidity_provider.py` posts resting
  orders to earn Kalshi's Liquidity Incentive Program rewards, which can offset
  the adverse selection that leaves plain market making at break-even. Earnings
  scale with capital.
- **Crypto mean-reversion.** `src/strategies/crypto_meanrev.py` implements a
  trend-filtered dip-buying strategy: buy BTC after a run of consecutive down
  days only while price is above its 200-day moving average, exit on the first up
  day. Backtested on about 5.8 years of daily data (bull and bear regimes), it
  wins roughly 65 to 73 percent of trades and is net positive after fees, robust
  across fee levels of 0.1 to 0.3 percent round trip. The trend filter is
  essential; the same dip buying loses money in downtrends. Trend-following and
  leverage were tested and ruled out (`src/paper/crypto_perp_backtest.py`):
  leverage is treated as a survival risk, not a return multiplier.

## Architecture

- `src/clients/` Kalshi v2 API client (RSA-signed auth, market data, orders,
  events, order books, candlesticks, balance) and LLM clients with a provider
  fallback chain.
- `src/strategies/` execution strategies (event convergence, liquidity provider).
- `src/paper/` backtests and analysis (event convergence, settled-outcome
  calibration, market making, crypto perps).
- `src/jobs/` live scanners and order execution.
- `src/utils/` exact fee math, fee-aware edge filtering, position sizing, and
  database helpers.

## Setup

```bash
pip install -r requirements.txt
cp env.template .env   # fill in your own keys
```

You need a Kalshi API key ID and an RSA private key (path set in `.env`).

## Risk controls

Trading is off by default and gated by independent switches that must be set
deliberately:

```
TRADING_HALTED=false      # master kill switch at the order layer
LIVE_TRADING_ENABLED=true
```

There is a hard kill switch at the order-placement function, fee-aware net-edge
filtering, a balance guard, and per-account and per-market caps. Each strategy
also runs in dry-run by default and requires its own live flag.

## Disclaimer

Trading carries real risk of loss. Nothing here is financial advice and there is
no guarantee of profit. Backtested results are not a promise of future
performance. Validate any strategy yourself at small size before committing
capital.
