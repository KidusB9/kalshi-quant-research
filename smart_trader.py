"""
Smart trader: AI-powered World Cup match analysis + limit order placement.
Scans upcoming World Cup matches, uses AI to estimate true probabilities,
and places small limit orders where edge exceeds fees.
"""
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from math import ceil

import httpx
from dotenv import load_dotenv

load_dotenv()

from src.clients.kalshi_client import KalshiClient

AI_MODEL = "openai/gpt-4.1"
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY")

MAX_POSITION_DOLLARS = 5.00
MIN_EDGE_AFTER_FEES = 0.04
MAX_TRADES = 8


def kalshi_taker_fee_per_contract(price: float) -> float:
    return ceil(0.07 * price * (1 - price) * 10000) / 10000


def kalshi_maker_fee_per_contract(price: float) -> float:
    return ceil(0.25 * 0.07 * price * (1 - price) * 10000) / 10000


async def ai_analyze(game_desc: str, yes_label: str, no_label: str, tie_label: str,
                     yes_price: float, no_price: float, tie_price: float) -> dict:
    """Ask AI to estimate true win/draw/lose probabilities for a World Cup match."""
    prompt = f"""You are a world-class sports betting analyst specializing in soccer/football.
Your job is to find MISPRICED markets — where the true probability differs from market consensus.

Match: {game_desc}
Current Kalshi market prices:
  {yes_label} WIN: {yes_price:.0%}
  {no_label} WIN: {no_price:.0%}
  DRAW: {tie_price:.0%}

Analyze deeply:
1. FIFA World Rankings (Dec 2025) and Elo ratings
2. Recent competitive match results (last 12 months)
3. World Cup 2026 qualifying performance
4. Squad strength: key players, injuries, depth
5. Historical head-to-head record
6. Tactical style matchup (attacking vs defensive, etc.)
7. Motivation factors (already qualified to next round? must-win?)
8. World Cup group stage historical patterns (draws are ~25% of WC group games)

IMPORTANT: Don't anchor to the market prices. Independently estimate the true probability, then compare to market.
World Cup group stage draws historically happen ~25% of the time.
Strong favorites at home World Cups historically overperform their ranking.
Mismatch games (top 10 vs 50+) see favorites winning ~70-80%.

Reply ONLY with valid JSON (no markdown):
{{"team_a_win_pct": <number 0-100>, "team_b_win_pct": <number 0-100>, "draw_pct": <number 0-100>, "confidence": <number 0-100>, "edge_reasoning": "<which outcome is most mispriced and why, 2-3 sentences>"}}

Percentages must sum to 100. Be bold — if you think the market is wrong, say so.
"""
    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"},
            json={
                "model": AI_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 300,
                "temperature": 0.1,
            },
        )
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0]
        return json.loads(content)


async def main():
    client = KalshiClient()
    try:
        # 1. Get balance
        bal_data = await client.get_balance()
        balance = float(bal_data.get("balance_dollars", bal_data.get("balance", 0)))
        if isinstance(balance, str):
            balance = float(balance)
        print(f"Account balance: ${balance:.2f}")
        print()

        if balance < 5:
            print("Balance too low to trade safely. Need at least $5.")
            return

        # 2. Find World Cup game markets
        cursor = None
        wc_games = {}
        for page in range(5):
            result = await client.get_markets(status="open", limit=200, series_ticker="KXWCGAME", cursor=cursor)
            batch = result.get("markets", [])
            cursor = result.get("cursor")
            for m in batch:
                tk = m.get("ticker", "")
                ya = float(m.get("yes_ask_dollars", "0") or "0")
                yb = float(m.get("yes_bid_dollars", "0") or "0")
                vol = float(m.get("volume_fp", "0") or "0")
                tl = m.get("title", "")
                close = m.get("close_time", "")
                sp = round(ya - yb, 4) if ya > 0 and yb > 0 else 9

                if vol >= 500 and sp <= 0.03 and ya > 0.05 and ya < 0.95:
                    # Extract game key (without outcome suffix)
                    parts = tk.rsplit("-", 1)
                    if len(parts) == 2:
                        game_key = parts[0]
                        outcome = parts[1]
                        if game_key not in wc_games:
                            wc_games[game_key] = {"title": tl, "close": close, "outcomes": {}}
                        wc_games[game_key]["outcomes"][outcome] = {
                            "ticker": tk,
                            "yes_ask": ya,
                            "yes_bid": yb,
                            "spread": sp,
                            "volume": int(vol),
                        }
            if not cursor or not batch:
                break

        print(f"Found {len(wc_games)} World Cup games with liquid markets")
        print()

        # 3. Filter to games with all 3 outcomes (win/lose/draw)
        tradeable = []
        for game_key, game in wc_games.items():
            outcomes = game["outcomes"]
            if len(outcomes) >= 3 and "TIE" in outcomes:
                team_codes = [k for k in outcomes if k != "TIE"]
                if len(team_codes) >= 2:
                    tradeable.append((game_key, game, team_codes))

        tradeable.sort(key=lambda x: sum(v["volume"] for v in x[1]["outcomes"].values()), reverse=True)
        print(f"Games with full 3-way markets: {len(tradeable)}")
        print()

        # 4. Analyze top games with AI
        trades_to_place = []
        analyzed = 0

        for game_key, game, team_codes in tradeable[:12]:
            t1, t2 = team_codes[0], team_codes[1]
            o1 = game["outcomes"][t1]
            o2 = game["outcomes"][t2]
            ot = game["outcomes"]["TIE"]

            game_desc = f"{t1} vs {t2} (World Cup 2026 Group Stage)"
            print(f"Analyzing: {game_desc}")
            print(f"  Market: {t1}={o1['yes_ask']:.0%}  {t2}={o2['yes_ask']:.0%}  TIE={ot['yes_ask']:.0%}")

            try:
                ai_result = await ai_analyze(
                    game_desc, t1, t2, "TIE",
                    o1["yes_ask"], o2["yes_ask"], ot["yes_ask"],
                )
                analyzed += 1
                t1_prob = ai_result["team_a_win_pct"] / 100
                t2_prob = ai_result["team_b_win_pct"] / 100
                tie_prob = ai_result["draw_pct"] / 100
                confidence = ai_result.get("confidence", 50) / 100
                reasoning = ai_result.get("reasoning", "")

                print(f"  AI says: {t1}={t1_prob:.0%}  {t2}={t2_prob:.0%}  TIE={tie_prob:.0%}  conf={confidence:.0%}")
                print(f"  Reason: {reasoning}")

                # Check each outcome for edge
                for label, prob, outcome_data in [
                    (t1, t1_prob, o1),
                    (t2, t2_prob, o2),
                    ("TIE", tie_prob, ot),
                ]:
                    market_price = outcome_data["yes_ask"]
                    bid_price = outcome_data["yes_bid"]
                    raw_edge = prob - market_price
                    maker_fee = kalshi_maker_fee_per_contract(market_price)
                    net_edge = raw_edge - maker_fee

                    # Also check NO side: if AI says low prob but market has high price
                    no_price = 1 - bid_price
                    no_prob = 1 - prob
                    no_edge = no_prob - no_price
                    no_fee = kalshi_maker_fee_per_contract(no_price)
                    no_net_edge = no_edge - no_fee

                    if net_edge >= MIN_EDGE_AFTER_FEES and confidence >= 0.55:
                        # BUY YES at slightly above best bid (maker order)
                        limit_price = bid_price + 0.01
                        contracts = min(int(MAX_POSITION_DOLLARS / limit_price), int(balance * 0.1 / limit_price))
                        if contracts >= 1:
                            trades_to_place.append({
                                "ticker": outcome_data["ticker"],
                                "side": "yes",
                                "action": "buy",
                                "price_cents": int(limit_price * 100),
                                "contracts": contracts,
                                "cost": round(limit_price * contracts, 2),
                                "edge": round(net_edge, 4),
                                "ai_prob": round(prob, 3),
                                "market_price": market_price,
                                "label": f"YES {label} ({game_key})",
                                "confidence": confidence,
                            })
                            print(f"  >>> TRADE: BUY YES {label} @ {limit_price:.2f} x{contracts} (edge={net_edge:.1%})")

                    elif no_net_edge >= MIN_EDGE_AFTER_FEES and confidence >= 0.55:
                        # BUY NO at slightly above best no bid
                        no_bid_price = 1 - outcome_data["yes_ask"]
                        no_limit = no_bid_price + 0.01
                        contracts = min(int(MAX_POSITION_DOLLARS / no_limit), int(balance * 0.1 / no_limit))
                        if contracts >= 1:
                            trades_to_place.append({
                                "ticker": outcome_data["ticker"],
                                "side": "no",
                                "action": "buy",
                                "price_cents": int(no_limit * 100),
                                "contracts": contracts,
                                "cost": round(no_limit * contracts, 2),
                                "edge": round(no_net_edge, 4),
                                "ai_prob": round(no_prob, 3),
                                "market_price": no_price,
                                "label": f"NO {label} ({game_key})",
                                "confidence": confidence,
                            })
                            print(f"  >>> TRADE: BUY NO {label} @ {no_limit:.2f} x{contracts} (edge={no_net_edge:.1%})")

                print()

            except Exception as e:
                print(f"  AI analysis failed: {e}")
                print()
                continue

        # 5. Summary
        print("=" * 60)
        print(f"ANALYSIS COMPLETE: {analyzed} games analyzed")
        print(f"TRADES FOUND: {len(trades_to_place)}")
        print()

        if not trades_to_place:
            print("No trades with sufficient edge found. Markets are efficiently priced.")
            print("This is actually a GOOD sign - means the bot won't take bad trades.")
            return

        # Sort by edge (highest first), take top MAX_TRADES
        trades_to_place.sort(key=lambda x: x["edge"], reverse=True)
        trades_to_place = trades_to_place[:MAX_TRADES]

        total_cost = sum(t["cost"] for t in trades_to_place)
        print(f"SELECTED TOP {len(trades_to_place)} TRADES (total cost: ${total_cost:.2f}):")
        for i, t in enumerate(trades_to_place, 1):
            print(f"  {i}. {t['label']}: {t['side'].upper()} @ ${t['price_cents']/100:.2f} x{t['contracts']} "
                  f"= ${t['cost']:.2f} (edge={t['edge']:.1%}, AI={t['ai_prob']:.0%}, mkt={t['market_price']:.0%})")

        if total_cost > balance * 0.5:
            print(f"\nWARNING: Total cost ${total_cost:.2f} > 50% of balance ${balance:.2f}. Reducing positions.")
            scale = (balance * 0.4) / total_cost
            for t in trades_to_place:
                t["contracts"] = max(1, int(t["contracts"] * scale))
                t["cost"] = round(t["price_cents"] / 100 * t["contracts"], 2)
            total_cost = sum(t["cost"] for t in trades_to_place)
            print(f"Scaled down to ${total_cost:.2f}")

        # 6. Place trades
        print()
        live = os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true"
        if not live:
            print("*** PAPER MODE - trades will NOT be placed on Kalshi ***")
            print("*** Set LIVE_TRADING_ENABLED=true in .env to go live ***")
            print()
            for t in trades_to_place:
                oid = str(uuid.uuid4())
                print(f"[PAPER] Would place: {t['side'].upper()} {t['ticker']} @ {t['price_cents']}c x{t['contracts']} (${t['cost']:.2f})")
        else:
            print("*** LIVE MODE - placing real orders on Kalshi ***")
            print()
            placed = 0
            for t in trades_to_place:
                oid = str(uuid.uuid4())
                try:
                    order_params = {
                        "ticker": t["ticker"],
                        "client_order_id": oid,
                        "side": t["side"],
                        "action": t["action"],
                        "count": t["contracts"],
                        "type_": "limit",
                    }
                    if t["side"] == "yes":
                        order_params["yes_price"] = t["price_cents"]
                    else:
                        order_params["no_price"] = t["price_cents"]

                    result = await client.place_order(**order_params)
                    order = result.get("order", result)
                    status = order.get("status", "unknown")
                    print(f"[LIVE] PLACED: {t['side'].upper()} {t['ticker']} @ {t['price_cents']}c x{t['contracts']} => {status}")
                    placed += 1
                except Exception as e:
                    print(f"[LIVE] FAILED: {t['ticker']} - {e}")

            print(f"\nPlaced {placed}/{len(trades_to_place)} orders")

        # 7. Check final state
        print()
        bal2 = await client.get_balance()
        print(f"Final balance: ${float(bal2.get('balance_dollars', '0')):.2f}")
        orders = await client.get_orders(status="resting")
        resting = orders.get("orders", [])
        if resting:
            print(f"Resting orders: {len(resting)}")
            for o in resting:
                print(f"  {o.get('ticker','')} {o.get('side','')} {o.get('action','')} @ {o.get('yes_price','')}/{o.get('no_price','')} x{o.get('remaining_count','')}")

    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
