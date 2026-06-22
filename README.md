# Kalshi Quant Research

This is a research codebase for trading and providing liquidity on Kalshi, a
CFTC-regulated event-contract exchange where contracts settle at $1 if an event
happens and $0 if it doesn't. It started as an automated directional bot and
turned into a systematic study of where (if anywhere) a retail account can
extract edge from the exchange. The short version: directional prediction does
not beat the market, structural arbitrage is not fillable, and the only positive
expected value I could find is liquidity provision under Kalshi's incentive
program.

The rest of this document is the actual methodology, the numbers, and the
reasons each approach worked or didn't. I'm documenting the failures in detail
because they're the useful part.

## How Kalshi pricing and fees work

A contract trades between $0.01 and $0.99 and pays $1.00 on a "yes" resolution.
So the price is the market's implied probability. Two facts drive everything
below.

First, the fee. Kalshi charges a per-contract trading fee of:

```
fee = ceil(0.07 * P * (1 - P) * 100) cents      (standard markets)
fee = ceil(0.035 * P * (1 - P) * 100) cents     (S&P / NASDAQ index markets)
```

where P is the price in dollars. Maker (resting) orders are charged a quarter of
the taker rate. The fee peaks at P = 0.50 (about 1.75 cents per contract per
side on a standard market) and shrinks toward the wings. A round trip at mid is
roughly 3.5 cents.

Second, and this is the part people skip: on a binary contract the probability
edge equals the per-contract dollar edge. If your true probability is q and you
buy "yes" at price p, expected value is `q*(1) - p = q - p`. So a "5% edge" is
literally 5 cents of EV per contract, before fees. That makes the break-even
math brutal and exact, which is why most of what follows fails.

## Methodology

Everything was tested against live and settled data pulled straight from the
Kalshi v2 API (`/markets`, `/markets/{ticker}`, `/events` with nested markets,
`/markets/{ticker}/orderbook`, and account balance). Two kinds of tests:

- Backtests against real settled outcomes. Settled markets expose a `result`
  field (`yes`/`no`) and `last_price_dollars` (the last traded price before
  settlement), so you can measure calibration: did markets priced at X% actually
  resolve "yes" X% of the time, and is any rule net positive after the real fee.
- Forward checks against the live order book. For any rule that looked good on
  paper, the deciding question was whether you can actually fill it at the price
  the backtest assumed. Usually you can't, and that's where the edges die.

A data-quality note that cost me real time and is worth flagging: the flat
`/markets` pagination is dominated by `KXMVE*` micro-markets (rapid-settling
multi-game contracts that resolve every few minutes), so a naive scan of the
first several thousand markets samples almost nothing tradeable. You have to go
through the `/events` endpoint, which groups by real event and surfaces the
liquid sports, weather, politics, and econ markets. Out of about 16,500 open
markets, roughly 11,900 are liquid and two-sided once you filter the micro-market
flood out.

## What I tested

### 1. LLM directional prediction. No edge.

The bot routes a market to one or more language models (through OpenRouter, plus
direct Gemini and DeepSeek with a fallback chain), gets a probability estimate,
and trades the gap versus the market price. I modeled the no-skill null directly:
draw outcomes from Bernoulli(market price) and let the "AI" estimate be the
market price plus noise. Under that null every trade loses about the fee, and a
Monte Carlo over real prices showed you need a genuine, repeatable edge of
roughly 18 to 20 percentage points just to clear fees and break even. A model
reading the same public information the market already priced does not have an
18-point edge. It has noise. The live logs agreed: small consistent bleed, one
fee at a time.

The fallback chain (Gemini, then DeepSeek, then OpenRouter, then abstain) is
still worth keeping because the old code would trade on stale or rate-limited
responses. But better plumbing does not manufacture an edge that isn't there.

### 2. Complete-set arbitrage. Not real.

For a set of mutually exclusive, collectively exhaustive outcomes, the "yes"
prices should sum to about $1 plus the vig. If they sum to less than $1 you could
buy every leg and lock a profit. Scanning by event grouping turns up hundreds of
groups summing below $1. They are all artifacts. Kalshi's `mutually_exclusive`
flag does not imply the listed legs are exhaustive, so the sum is below $1
because there is an unlisted outcome (an "other candidate," a draw, a field) that
can win. The missing mass is priced risk, not free money. After requiring the
legs to plausibly span the outcome space and checking the live asks, the only
"locks" left were two-leg races netting half a cent to two cents on one to nine
contracts of size, which is noise, not a strategy.

### 3. Favorite-longshot fade. Real bias, unfillable.

Cheap longshots are overpriced. In settled liquid markets, contracts priced
around 13% "yes" resolved "yes" close to 0% of the time, which is a large,
genuine calibration gap and matches the academic favorite-longshot literature.
The trade is to buy "no." The problem is the book: on these longshots the "no"
ask sits 6 to 8 cents above fair value (`1 - yes_price`). A backtest using the
last traded price shows about +1.3 cents per contract, but you cannot transact at
that price. Buying "no" at the real ask is negative every time I sampled it. The
bias is real and the trade is not.

### 4. Plain market making. Break-even on its own.

Across roughly 11,900 liquid two-sided markets the median spread is about 4 cents
and 99% of them have a spread wider than the round-trip maker fee, so the gross
capture looks great (median around 3.8 cents net of fees). That number is gross
of adverse selection, which is the whole game. Using each market's own recent
mid movement as a lower-bound estimate of pick-off cost, the net drops to roughly
break-even (median about +1.3 cents, mean about 0). And that estimate is
optimistic, because a one-step mid move understates the adverse fill you take
over a full quote lifetime. On its own, spread capture does not clear adverse
selection on this exchange.

### 5. Liquidity Incentive Program harvesting. The positive one.

Kalshi runs a Liquidity Incentive Program that pays you for resting orders near
the best bid/ask. It snapshots the book roughly every second, scores each order
by `size * distance_multiplier` (full credit at the best price, decaying with a
discount factor as you quote away from it), and pays you a share of a daily pool:

```
your_reward = (your_score / total_score_of_all_participants) * reward_pool
```

Pools run from $10 to $1,000 per day per active market. The key property is that
you get paid for resting liquidity whether or not your order fills. That reward
is exactly what offsets the adverse-selection cost that left plain market making
at break-even. This is the only approach in this repo with genuine positive
expected value.

The honest constraints: your payout is a share of a pool, so a small account
earns a small share (realistically single-digit dollars a day at low capital),
the reward schedule per market is shown on the market page rather than the API
endpoints I used, and you still carry inventory risk when your resting orders
fill. The harvester here targets slow, tight, liquid markets to minimize that
fill risk, posts at the best bid/ask within hard capital caps, and runs in
dry-run by default.

## The takeaway

Kalshi is efficient wherever it has liquidity. Directional prediction does not
beat it, arbitrage that looks free is priced risk, and the favorite-longshot
bias is real but blocked by the spread. The only thing that pays is being a
liquidity provider and collecting incentive rewards, and that is a low-margin
grind that scales with capital, not a prediction engine. If something is being
sold to you as an AI that forecasts these markets for profit, it is a story.

## Architecture

- `src/clients/` Kalshi v2 API client (RSA-signed auth, market data, orders,
  events, order books, balance) and LLM clients for OpenRouter, Gemini, and
  DeepSeek with an ordered fallback chain that abstains when every provider
  fails.
- `src/jobs/arbitrage_scan.py` Live complete-set arbitrage scanner using real
  event groupings and real asks, with an exhaustiveness guard.
- `src/paper/settled_backtest.py` Calibration and after-fee rule backtest against
  real settled outcomes.
- `src/paper/backtest.py` Monte Carlo over real prices, including the break-even
  edge sweep.
- `src/paper/market_maker_paper.py` Spread distribution and adverse-selection
  analysis.
- `src/paper/longshot_paper_trader.py` Longshot-fade forward test against the
  live book.
- `src/strategies/liquidity_provider.py` The incentive-program harvester.
- `src/utils/` Exact Kalshi fee math, fee-aware edge filtering, position sizing,
  and database helpers.

## Setup

```bash
pip install -r requirements.txt
cp env.template .env   # fill in your own keys
```

You need a Kalshi API key ID and an RSA private key (path set in `.env`). LLM
keys are optional and only relevant to the directional path, which loses money.

## Risk controls

Trading is off by default and gated by two independent switches:

```
TRADING_HALTED=false      # master kill switch at the order layer; blocks all new positions when true
LIVE_TRADING_ENABLED=true
```

There is a hard kill switch at the order-placement function (so no strategy or
entry point can place a live order around it), fee-aware net-edge filtering, a
balance guard, and per-account and per-market caps. Leave the defaults until a
strategy is validated against real outcomes.

## Disclaimer

Trading carries real risk of loss. Nothing here is financial advice and there is
no guarantee of profit. Most of what is documented above is a record of
approaches that lost money or broke even. Validate anything yourself before
risking capital, and assume the market is better informed than your model.
