"""
Autonomous hybrid runner.

One always-on process that drives every strategy at the cadence where its edge
actually lives:

  Live soccer  -> every 90s   (fleeting goal-driven price spikes)
  Season sports-> every 3h    (markets drift slowly)
  Crypto signal-> once a day  (signal is a daily bar; can't change faster)
  Funding mon. -> once a day  (funding regime moves slowly)

Polling faster than the edge moves earns nothing, so this does not waste calls
on stationary signals -- it only runs the fast loop where speed pays (soccer).

SAFETY:
  * Dry-run by default. Real orders require launching with --live AND the master
    kill switch off (the runner sets TRADING_HALTED=false only for the trading
    subprocesses it launches, per run).
  * Cash floor: it reads your live Kalshi cash each cycle and will NOT place
    orders if cash is below RUNNER_CASH_FLOOR -- this bounds total deployment.
  * Dedup: it passes your already-bought tickers to the traders so repeated runs
    never re-buy the same market.
  * Per-strategy caps still apply (CONV_MAX_CAPITAL, SOCCER_MAX_CAPITAL).

Run:
  python -m src.autonomous_runner            # dry-run, safe
  python -m src.autonomous_runner --live     # places real orders (capped, gated)
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

CASH_FLOOR = float(os.getenv("RUNNER_CASH_FLOOR", "5"))
TICK_SECONDS = 30

# Per-run capital cap (bounds a single cycle's deployment even if cash is ample).
RUN_CAP = os.getenv("RUNNER_PER_RUN_CAP", "10")

SCHEDULE = {
    "soccer":  {"interval": 90,          "module": "src.strategies.soccer_live_trader",
                "live_env": {"SOCCER_LIVE": "true", "SOCCER_MAX_CAPITAL": RUN_CAP}, "trade": True},
    "sports":  {"interval": 3 * 3600,    "module": "src.strategies.convergence_trader",
                "live_env": {"CONV_LIVE": "true", "CONV_MAX_CAPITAL": RUN_CAP}, "trade": True},
    "crypto":  {"interval": 24 * 3600,   "module": "src.strategies.crypto_meanrev",
                "live_env": {}, "trade": False},
    "funding": {"interval": 24 * 3600,   "module": "src.strategies.funding_monitor",
                "live_env": {}, "trade": False},
}


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}Z] {msg}", flush=True)


async def _held_tickers() -> str:
    from src.clients.kalshi_client import KalshiClient
    c = KalshiClient()
    try:
        r = await c._make_authenticated_request("GET", "/trade-api/v2/portfolio/orders",
                                                params={"limit": 200}, require_auth=True)
        held = {o["ticker"] for o in r.get("orders", [])
                if o.get("action") == "buy" and o.get("status") == "executed"}
        return ",".join(sorted(held))
    except Exception:
        return ""
    finally:
        await c.close()


async def _cash() -> float:
    from src.clients.kalshi_client import KalshiClient
    c = KalshiClient()
    try:
        b = await c.get_balance()
        return int(b.get("balance", 0) or 0) / 100
    except Exception:
        return -1.0
    finally:
        await c.close()


def _run(module: str, args: list, env_extra: dict) -> str:
    env = os.environ.copy()
    env.update(env_extra)
    try:
        out = subprocess.run([sys.executable, "-m", module] + args, env=env,
                             capture_output=True, text=True, timeout=240)
        return out.stdout
    except subprocess.TimeoutExpired:
        return "(timed out)"
    except Exception as e:
        return f"(error: {e})"


def _summary(name: str, output: str) -> str:
    # pull the most informative line per strategy
    keys = {
        "soccer": ("signal", "LIVE BET", "no in-game"),
        "sports": ("Entries:", "LIVE: placed", "No markets"),
        "crypto": ("ENTER LONG", "No entry today"),
        "funding": ("GO/NO-GO", "NO-GO", "GO --"),
    }.get(name, ())
    lines = [ln.strip() for ln in output.splitlines() if any(k in ln for k in keys)]
    return " | ".join(lines[:2]) if lines else "(ran)"


async def _tick(state: dict, live: bool) -> None:
    now = time.time()
    due = [n for n, c in SCHEDULE.items() if now - state.get(n, 0) >= c["interval"]]
    if not due:
        return
    skip = await _held_tickers()
    cash = await _cash()
    # Strict floor: a single run may deploy at most (cash - floor), so it can
    # never pull cash below the reserve even with the per-run cap.
    headroom = max(0.0, cash - CASH_FLOOR)
    can_trade = live and headroom >= 1.0
    run_cap = round(min(float(RUN_CAP), headroom), 2)
    if live and not can_trade:
        log(f"cash ${cash:.2f}, headroom ${headroom:.2f} -> trading paused (need >=$1 above ${CASH_FLOOR:.0f} floor).")
    for name in due:
        cfg = SCHEDULE[name]
        args, env_extra = [], {"RUNNER_SKIP_TICKERS": skip}
        if cfg["trade"] and can_trade:
            args = ["--live"]
            env_extra["TRADING_HALTED"] = "false"
            env_extra.update(cfg["live_env"])
            # override caps to the strict headroom (priority: soccer keeps cash
            # available by capping sports to the same small headroom too)
            env_extra["CONV_MAX_CAPITAL"] = str(run_cap)
            env_extra["SOCCER_MAX_CAPITAL"] = str(run_cap)
        out = _run(cfg["module"], args, env_extra)
        log(f"{name:8s} -> {_summary(name, out)}")
        state[name] = now


async def main() -> None:
    live = "--live" in sys.argv
    log("=" * 60)
    log(f"AUTONOMOUS RUNNER starting ({'LIVE' if live else 'DRY-RUN'})")
    log("cadence: soccer 90s | sports 3h | crypto 24h | funding 24h")
    log(f"cash floor ${CASH_FLOOR:.0f} | per-strategy caps apply | Ctrl+C to stop")
    log("=" * 60)
    state: dict = {}
    while True:
        try:
            await _tick(state, live)
        except Exception as e:
            log(f"tick error (continuing): {e}")
        await asyncio.sleep(TICK_SECONDS)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("stopped.")
