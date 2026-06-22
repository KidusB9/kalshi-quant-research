"""Quick portfolio status check. Run: python check_status.py"""
import asyncio
import json
from dotenv import load_dotenv

load_dotenv()
from src.clients.kalshi_client import KalshiClient


async def status():
    client = KalshiClient()
    try:
        bal = await client.get_balance()
        cash = float(bal.get("balance_dollars", "0"))

        pos = await client.get_positions()
        positions = pos.get("market_positions", pos.get("positions", []))

        total_pnl = 0
        total_invested = 0

        print("=" * 80)
        print("KALSHI PORTFOLIO STATUS")
        print("=" * 80)
        print(f"{'Ticker':<45} {'Side':>5} {'Qty':>4} {'Entry':>7} {'Now':>7} {'P&L':>8}")
        print("-" * 80)

        for p in positions:
            tk = p.get("ticker", "")
            pos_count = float(p.get("position_fp", "0") or "0")
            if pos_count == 0:
                continue
            side = "YES" if pos_count > 0 else "NO"
            abs_count = abs(int(pos_count))
            exposure = float(p.get("market_exposure_dollars", "0") or "0")
            entry = exposure / abs_count if abs_count > 0 else 0

            mk = await client.get_market(tk)
            m = mk.get("market", mk)
            ya = float(m.get("yes_ask_dollars", "0") or "0")
            yb = float(m.get("yes_bid_dollars", "0") or "0")
            mid = (ya + yb) / 2
            result = m.get("result", "")

            if result:
                won = (side == "YES" and result == "yes") or (side == "NO" and result == "no")
                pnl = ((1.0 - entry) if won else -entry) * abs_count
                now_str = f"{'WIN' if won else 'LOSS':>7}"
            else:
                now_price = mid if side == "YES" else (1 - mid)
                unrealized = (mid - entry) if side == "YES" else ((1 - mid) - entry)
                pnl = unrealized * abs_count
                now_str = f"{now_price:>7.3f}"

            total_pnl += pnl
            total_invested += entry * abs_count
            rpnl = float(p.get("realized_pnl_dollars", "0") or "0")
            fees = float(p.get("fees_paid_dollars", "0") or "0")

            print(f"{tk:<45} {side:>5} {abs_count:>4} {entry:>7.3f} {now_str} {pnl:>+8.2f}")

        print("-" * 80)
        print(f"Cash: ${cash:.2f}  |  Invested: ${total_invested:.2f}  |  Unrealized P&L: ${total_pnl:+.2f}")
        print(f"Total equity: ${cash + total_invested + total_pnl:.2f}")
        print("=" * 80)

        # Recent log entries
        print("\nRecent bot activity:")
        try:
            with open("logs/auto_trader.log", "r") as f:
                lines = f.readlines()
                for line in lines[-10:]:
                    print(f"  {line.rstrip()}")
        except FileNotFoundError:
            print("  No log file found. Is the bot running?")

    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(status())
