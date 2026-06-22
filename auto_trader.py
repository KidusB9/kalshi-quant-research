"""
Autonomous Kalshi Trading Bot
Runs continuously: scans markets, analyzes with AI, places trades, monitors positions.
Launch with: python auto_trader.py
Stop with: Ctrl+C
"""
import asyncio
import json
import logging
import os
import signal
import sys
import uuid
from datetime import datetime, timezone
from math import ceil
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

from src.clients.kalshi_client import KalshiClient

# ── Configuration ──────────────────────────────────────────────────────────
SCAN_INTERVAL_SECONDS = 900       # 15 minutes between scans
POSITION_CHECK_SECONDS = 300      # 5 minutes between position checks
AI_MODEL = "deepseek/deepseek-chat-v3-0324"  # cheap: ~$0.01/day
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY")

MAX_POSITIONS = 15                # max concurrent positions
MAX_COST_PER_TRADE = 5.00         # max $ per single trade
MIN_BALANCE_RESERVE = 10.00       # never go below this
MIN_EDGE_AFTER_FEES = 0.04        # 4% minimum edge after fees
MIN_AI_CONFIDENCE = 0.55          # 55% AI confidence floor
MAX_SPREAD = 0.03                 # max 3 cent bid-ask spread
MIN_VOLUME = 500                  # minimum contract volume
TAKE_PROFIT_CENTS = 0.08          # take profit at +8 cents
STOP_LOSS_CENTS = 0.10            # stop loss at -10 cents
MAX_AI_CALLS_PER_SCAN = 12        # cap AI spend per scan cycle

SERIES_TO_SCAN = [
    "KXWCGAME", "KXMLB", "KXNBA", "KXNFL",
    "KXFED", "KXBTC", "KXINX", "KXCPI",
]

# ── Logging ────────────────────────────────────────────────────────────────
Path("logs").mkdir(exist_ok=True)

logger = logging.getLogger("auto_trader")
logger.setLevel(logging.INFO)

fh = logging.FileHandler("logs/auto_trader.log", encoding="utf-8")
fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(fh)

ch = logging.StreamHandler(sys.stdout)
ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(ch)

# ── Helpers ────────────────────────────────────────────────────────────────
shutdown = asyncio.Event()


def kalshi_maker_fee(price: float) -> float:
    return ceil(0.25 * 0.07 * price * (1 - price) * 10000) / 10000


async def ai_analyze_match(game_desc: str, outcomes: dict) -> dict | None:
    labels = list(outcomes.keys())
    prices = {k: v["yes_ask"] for k, v in outcomes.items()}
    price_lines = "\n".join(f"  {k}: {v:.0%}" for k, v in prices.items())

    prompt = f"""You are a sports/event prediction expert. Analyze this market and estimate true probabilities.

Event: {game_desc}
Current Kalshi market prices:
{price_lines}

For sports: consider FIFA/Elo rankings, recent form, squad quality, historical patterns, motivation.
For economics: consider recent data trends, Fed guidance, consensus forecasts.

IMPORTANT: Don't anchor to market prices. Give your independent estimate.
World Cup group stage draws historically happen ~25% of the time.

Reply ONLY with valid JSON (no markdown):
{{"probabilities": {{{", ".join(f'"{k}": <number 0-100>' for k in labels)}}}, "confidence": <number 0-100>, "best_bet": "<which outcome is most mispriced>", "reasoning": "<1-2 sentences>"}}

Probabilities must sum to 100.
"""
    try:
        async with httpx.AsyncClient(timeout=90) as http:
            resp = await http.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_KEY}",
                         "Content-Type": "application/json"},
                json={"model": AI_MODEL,
                      "messages": [{"role": "user", "content": prompt}],
                      "max_tokens": 300, "temperature": 0.1},
            )
            data = resp.json()
            content = data["choices"][0]["message"]["content"].strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[1].rsplit("```", 1)[0]
            return json.loads(content)
    except Exception as e:
        logger.warning(f"AI analysis failed: {e}")
        return None


async def place_limit_buy(client: KalshiClient, ticker: str, side: str,
                          price_dollars: float, count: int) -> dict | None:
    """Place a V2 limit buy order. Returns order result or None on failure."""
    oid = str(uuid.uuid4())
    v2_side = "bid" if side == "yes" else "ask"
    v2_price = f"{price_dollars:.4f}" if side == "yes" else f"{1 - price_dollars:.4f}"

    body = {
        "ticker": ticker,
        "client_order_id": oid,
        "side": v2_side,
        "count": f"{count:.2f}",
        "price": v2_price,
        "time_in_force": "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross",
        "post_only": False,
        "cancel_order_on_pause": False,
        "reduce_only": False,
    }
    try:
        result = await client._make_authenticated_request(
            "POST", "/trade-api/v2/portfolio/events/orders", json_data=body
        )
        return result
    except Exception as e:
        logger.error(f"Order failed {ticker} {side}@{price_dollars}: {e}")
        return None


async def place_limit_sell(client: KalshiClient, ticker: str,
                           price_dollars: float, count: int) -> dict | None:
    oid = str(uuid.uuid4())
    body = {
        "ticker": ticker,
        "client_order_id": oid,
        "side": "ask",
        "count": f"{count:.2f}",
        "price": f"{price_dollars:.4f}",
        "time_in_force": "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross",
        "post_only": False,
        "cancel_order_on_pause": False,
        "reduce_only": False,
    }
    try:
        result = await client._make_authenticated_request(
            "POST", "/trade-api/v2/portfolio/events/orders", json_data=body
        )
        return result
    except Exception as e:
        logger.error(f"Sell order failed {ticker}@{price_dollars}: {e}")
        return None


# ── Core Bot Logic ─────────────────────────────────────────────────────────

class AutoTrader:
    def __init__(self):
        self.client: KalshiClient | None = None
        self.positions: dict = {}   # ticker -> {entry_price, count, side, placed_at}
        self.trades_today: int = 0
        self.last_scan: datetime | None = None
        self.total_pnl: float = 0.0

    async def start(self):
        self.client = KalshiClient()
        logger.info("=" * 60)
        logger.info("AUTONOMOUS TRADER STARTED")
        logger.info("=" * 60)

        await self._load_existing_positions()
        await self._print_status()

        scan_task = asyncio.create_task(self._scan_loop())
        monitor_task = asyncio.create_task(self._monitor_loop())

        await shutdown.wait()

        scan_task.cancel()
        monitor_task.cancel()
        try:
            await asyncio.gather(scan_task, monitor_task, return_exceptions=True)
        except asyncio.CancelledError:
            pass

        await self.client.close()
        logger.info("Bot shut down gracefully.")

    async def _load_existing_positions(self):
        """Load positions from Kalshi API using V2 response format.

        Uses market_exposure_dollars / position count as the average entry price.
        This is reliable for both YES and NO positions regardless of fill format.
        """
        pos = await self.client.get_positions()
        positions = pos.get("market_positions", pos.get("positions", []))

        for p in positions:
            tk = p.get("ticker", "")
            if not tk:
                continue
            pos_count = float(p.get("position_fp", "0") or "0")
            if pos_count == 0:
                continue

            side = "yes" if pos_count > 0 else "no"
            abs_count = abs(int(pos_count))
            exposure = float(p.get("market_exposure_dollars", "0") or "0")
            entry_price = exposure / abs_count if abs_count > 0 else 0.50

            self.positions[tk] = {
                "entry_price": round(entry_price, 4),
                "count": abs_count,
                "side": side,
                "placed_at": p.get("last_updated_ts", datetime.now(timezone.utc).isoformat()),
            }

        logger.info(f"Loaded {len(self.positions)} existing positions")

    async def _print_status(self):
        bal = await self.client.get_balance()
        cash = float(bal.get("balance_dollars", "0"))
        port_val = bal.get("portfolio_value", 0)
        if isinstance(port_val, (int, float)) and port_val > 100:
            port_val = port_val / 100
        logger.info(f"Cash: ${cash:.2f} | Positions: {len(self.positions)} | P&L: ${self.total_pnl:+.2f}")
        for tk, info in self.positions.items():
            logger.info(f"  {tk}: {info['side']} @ ${info['entry_price']:.2f} x{info['count']}")

    # ── Market Scanning ────────────────────────────────────────────────────

    async def _scan_loop(self):
        while not shutdown.is_set():
            try:
                await self._run_scan()
            except Exception as e:
                logger.error(f"Scan error: {e}", exc_info=True)
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=SCAN_INTERVAL_SECONDS)
                break
            except asyncio.TimeoutError:
                pass

    async def _run_scan(self):
        logger.info("--- MARKET SCAN STARTING ---")
        self.last_scan = datetime.now(timezone.utc)

        bal = await self.client.get_balance()
        cash = float(bal.get("balance_dollars", "0"))
        available = cash - MIN_BALANCE_RESERVE

        if available < 2.0:
            logger.info(f"Cash ${cash:.2f} too low (reserve=${MIN_BALANCE_RESERVE}). Skipping scan.")
            return

        if len(self.positions) >= MAX_POSITIONS:
            logger.info(f"At max positions ({MAX_POSITIONS}). Skipping scan.")
            return

        # Gather candidate markets
        candidates = await self._find_candidates()
        logger.info(f"Found {len(candidates)} candidate games to analyze")

        if not candidates:
            logger.info("No candidates found this scan.")
            return

        # Analyze with AI
        trades_found = []
        ai_calls = 0

        for game_key, game_data in candidates:
            if ai_calls >= MAX_AI_CALLS_PER_SCAN:
                break
            if game_key in [tk.rsplit("-", 1)[0] for tk in self.positions]:
                continue  # already have a position in this game

            result = await ai_analyze_match(game_data["desc"], game_data["outcomes"])
            ai_calls += 1

            if not result:
                continue

            probs = result.get("probabilities", {})
            confidence = result.get("confidence", 0) / 100
            reasoning = result.get("reasoning", "")

            if confidence < MIN_AI_CONFIDENCE:
                continue

            for label, ai_prob_pct in probs.items():
                if label not in game_data["outcomes"]:
                    continue
                ai_prob = ai_prob_pct / 100
                od = game_data["outcomes"][label]
                market_ask = od["yes_ask"]
                market_bid = od["yes_bid"]

                # YES side edge
                raw_edge = ai_prob - market_ask
                fee = kalshi_maker_fee(market_ask)
                net_edge = raw_edge - fee

                if net_edge >= MIN_EDGE_AFTER_FEES:
                    limit_price = market_bid + 0.01
                    max_contracts = min(
                        int(MAX_COST_PER_TRADE / limit_price),
                        int(available / limit_price),
                    )
                    if max_contracts >= 1:
                        trades_found.append({
                            "ticker": od["ticker"],
                            "side": "yes",
                            "price": limit_price,
                            "contracts": min(max_contracts, 10),
                            "edge": net_edge,
                            "ai_prob": ai_prob,
                            "market": market_ask,
                            "label": f"{label} ({game_key})",
                            "confidence": confidence,
                            "reasoning": reasoning,
                        })

                # NO side edge
                no_market = 1 - market_bid
                no_prob = 1 - ai_prob
                no_edge = no_prob - no_market - kalshi_maker_fee(no_market)
                if no_edge >= MIN_EDGE_AFTER_FEES:
                    no_limit = (1 - market_ask) + 0.01
                    max_c = min(int(MAX_COST_PER_TRADE / no_limit), int(available / no_limit))
                    if max_c >= 1:
                        trades_found.append({
                            "ticker": od["ticker"],
                            "side": "no",
                            "price": no_limit,
                            "contracts": min(max_c, 10),
                            "edge": no_edge,
                            "ai_prob": no_prob,
                            "market": no_market,
                            "label": f"NO {label} ({game_key})",
                            "confidence": confidence,
                            "reasoning": reasoning,
                        })

        if not trades_found:
            logger.info("No trades with sufficient edge this scan.")
            return

        # Sort by edge, take best
        trades_found.sort(key=lambda x: x["edge"], reverse=True)
        slots = MAX_POSITIONS - len(self.positions)
        trades_found = trades_found[:slots]

        # Cap total spend
        total_cost = sum(t["price"] * t["contracts"] for t in trades_found)
        if total_cost > available:
            scale = available / total_cost * 0.9
            for t in trades_found:
                t["contracts"] = max(1, int(t["contracts"] * scale))

        # Place orders
        for t in trades_found:
            cost = t["price"] * t["contracts"]
            logger.info(
                f"PLACING: {t['side'].upper()} {t['label']} @ ${t['price']:.2f} "
                f"x{t['contracts']} = ${cost:.2f} (edge={t['edge']:.1%}, "
                f"AI={t['ai_prob']:.0%}, mkt={t['market']:.0%})"
            )
            logger.info(f"  Reason: {t['reasoning']}")

            result = await place_limit_buy(
                self.client, t["ticker"], t["side"], t["price"], t["contracts"]
            )

            if result:
                filled = result.get("fill_count", "0")
                remaining = result.get("remaining_count", "0")
                logger.info(f"  => FILLED={filled} REMAINING={remaining}")

                filled_f = float(filled) if filled else 0
                if filled_f > 0:
                    self.positions[t["ticker"]] = {
                        "entry_price": t["price"],
                        "count": int(filled_f),
                        "side": t["side"],
                        "placed_at": datetime.now(timezone.utc).isoformat(),
                    }
                    self.trades_today += 1
            else:
                logger.warning(f"  => FAILED to place order")

        await self._print_status()
        logger.info("--- SCAN COMPLETE ---")

    async def _find_candidates(self) -> list:
        """Find liquid games/events with tight spreads."""
        games = {}
        for series in SERIES_TO_SCAN:
            cursor = None
            for page in range(3):
                try:
                    result = await self.client.get_markets(
                        status="open", limit=200, series_ticker=series, cursor=cursor
                    )
                except Exception:
                    break
                batch = result.get("markets", [])
                cursor = result.get("cursor")
                for m in batch:
                    tk = m.get("ticker", "")
                    ya = float(m.get("yes_ask_dollars", "0") or "0")
                    yb = float(m.get("yes_bid_dollars", "0") or "0")
                    vol = float(m.get("volume_fp", "0") or "0")
                    tl = m.get("title", "")
                    sp = round(ya - yb, 4) if ya > 0 and yb > 0 else 9

                    if vol < MIN_VOLUME or sp > MAX_SPREAD or ya < 0.05 or ya > 0.95:
                        continue

                    parts = tk.rsplit("-", 1)
                    if len(parts) == 2:
                        gk = parts[0]
                        outcome = parts[1]
                        if gk not in games:
                            games[gk] = {"desc": tl, "outcomes": {}}
                        games[gk]["outcomes"][outcome] = {
                            "ticker": tk, "yes_ask": ya, "yes_bid": yb,
                            "spread": sp, "volume": int(vol),
                        }
                if not cursor or not batch:
                    break

        # Filter to games with 2+ outcomes
        result = []
        for gk, gd in games.items():
            if len(gd["outcomes"]) >= 2:
                total_vol = sum(v["volume"] for v in gd["outcomes"].values())
                result.append((gk, gd))
        result.sort(key=lambda x: sum(v["volume"] for v in x[1]["outcomes"].values()), reverse=True)
        return result[:20]

    # ── Position Monitoring ────────────────────────────────────────────────

    async def _monitor_loop(self):
        while not shutdown.is_set():
            try:
                await self._check_positions()
            except Exception as e:
                logger.error(f"Monitor error: {e}", exc_info=True)
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=POSITION_CHECK_SECONDS)
                break
            except asyncio.TimeoutError:
                pass

    async def _check_positions(self):
        if not self.positions:
            return

        to_remove = []
        for tk, info in list(self.positions.items()):
            try:
                mk = await self.client.get_market(tk)
                market = mk.get("market", mk)
                status = market.get("status", "")
                result = market.get("result", "")
                ya = float(market.get("yes_ask_dollars", "0") or "0")
                yb = float(market.get("yes_bid_dollars", "0") or "0")
                mid = (ya + yb) / 2 if ya > 0 and yb > 0 else 0

                entry = info["entry_price"]
                count = info["count"]
                side = info["side"]

                # 1. Market resolved
                if result and result != "":
                    won = (side == "yes" and result == "yes") or (side == "no" and result == "no")
                    if won:
                        pnl = (1.0 - entry) * count
                        logger.info(f"WIN: {tk} resolved {result} | P&L: +${pnl:.2f}")
                    else:
                        pnl = -entry * count
                        logger.info(f"LOSS: {tk} resolved {result} | P&L: -${abs(pnl):.2f}")
                    self.total_pnl += pnl
                    to_remove.append(tk)
                    continue

                if status != "active":
                    continue

                # Compute unrealized P&L based on side
                if mid <= 0:
                    continue
                if side == "yes":
                    unrealized = mid - entry  # YES price going up = profit
                else:
                    no_mid = 1 - mid
                    unrealized = no_mid - entry  # NO value going up = profit

                # 2. Take profit
                if unrealized >= TAKE_PROFIT_CENTS:
                    if side == "yes":
                        sell_price = yb
                        logger.info(f"TAKE PROFIT: {tk} YES entry=${entry:.2f} now=${mid:.2f} (+${unrealized:.2f})")
                        res = await place_limit_sell(self.client, tk, sell_price, count)
                    else:
                        buy_back_price = ya
                        logger.info(f"TAKE PROFIT: {tk} NO entry=${entry:.2f} now=${mid:.2f} (+${unrealized:.2f})")
                        res = await place_limit_buy(self.client, tk, "yes", buy_back_price, count)
                    if res and float(res.get("fill_count", "0")) > 0:
                        pnl = unrealized * count
                        self.total_pnl += pnl
                        logger.info(f"  Closed | P&L: +${pnl:.2f}")
                        to_remove.append(tk)
                    continue

                # 3. Stop loss
                if unrealized <= -STOP_LOSS_CENTS:
                    if side == "yes":
                        sell_price = yb
                        logger.info(f"STOP LOSS: {tk} YES entry=${entry:.2f} now=${mid:.2f} (${unrealized:.2f})")
                        res = await place_limit_sell(self.client, tk, sell_price, count)
                    else:
                        buy_back_price = ya
                        logger.info(f"STOP LOSS: {tk} NO entry=${entry:.2f} now=${mid:.2f} (${unrealized:.2f})")
                        res = await place_limit_buy(self.client, tk, "yes", buy_back_price, count)
                    if res and float(res.get("fill_count", "0")) > 0:
                        pnl = unrealized * count
                        self.total_pnl += pnl
                        logger.info(f"  Closed | P&L: ${pnl:.2f}")
                        to_remove.append(tk)
                    continue

            except Exception as e:
                logger.warning(f"Error checking {tk}: {e}")

        for tk in to_remove:
            self.positions.pop(tk, None)

        if to_remove:
            await self._print_status()


# ── Entry Point ────────────────────────────────────────────────────────────

def handle_signal(sig, frame):
    logger.info(f"Received signal {sig}, shutting down...")
    shutdown.set()


async def main():
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    bot = AutoTrader()
    await bot.start()


if __name__ == "__main__":
    print("Starting Autonomous Kalshi Trader...")
    print("Press Ctrl+C to stop")
    print(f"Logs: logs/auto_trader.log")
    print()
    asyncio.run(main())
