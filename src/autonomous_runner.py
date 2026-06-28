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
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

CASH_FLOOR = float(os.getenv("RUNNER_CASH_FLOOR", "5"))
TICK_SECONDS = 20
STATE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "runner_state.json")


def _load_state() -> dict:
    """Persist last-run times across restarts so a relaunch does NOT re-fire a
    strategy that already ran within its interval (the over-deploy bug)."""
    if os.path.exists(STATE_PATH):
        try:
            return {k: float(v) for k, v in json.load(open(STATE_PATH)).items()}
        except Exception:
            return {}
    return {}


def _save_state(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        json.dump(state, open(STATE_PATH, "w"))
    except Exception:
        pass

# Per-run capital caps. Sports is the PROVEN edge so it stays primary, but is
# slowed and made to respect a soccer reserve so it stops hogging all the cash.
# Soccer is UNPROVEN -- the reserve is dry powder to TEST it during the World Cup
# without sacrificing the proven sports edge. Set SOCCER_RESERVE=0 to disable
# (e.g. between the Cup ending and the domestic leagues starting).
SPORTS_CAP = float(os.getenv("RUNNER_SPORTS_CAP", "4"))
SOCCER_CAP = float(os.getenv("RUNNER_SOCCER_CAP", "6"))
SOCCER_RESERVE = float(os.getenv("RUNNER_SOCCER_RESERVE", "10"))

SCHEDULE = {
    # exit runs fastest: it SELLS positions that turned against us. Sells are
    # always allowed (kill switch only blocks buys) and recover cash, so it is
    # NOT gated by the cash floor.
    "exit":    {"interval": 20,          "module": "src.strategies.exit_monitor",
                "live_env": {"EXIT_LIVE": "true"}, "kind": "sell"},
    "soccer":  {"interval": 45,          "module": "src.strategies.soccer_live_trader",
                "live_env": {"SOCCER_LIVE": "true"}, "kind": "buy"},
    "sports":  {"interval": 3 * 3600,    "module": "src.strategies.convergence_trader",
                "live_env": {"CONV_LIVE": "true"}, "kind": "buy"},
    "crypto":  {"interval": 24 * 3600,   "module": "src.strategies.crypto_meanrev",
                "live_env": {}, "kind": "signal"},
    "funding": {"interval": 24 * 3600,   "module": "src.strategies.funding_monitor",
                "live_env": {}, "kind": "signal"},
}


LOCK_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "runner.lock")
_lock_fh = None


def _acquire_single_instance_lock() -> bool:
    """OS-level exclusive lock so only ONE runner can trade at a time. The lock
    is auto-released by the OS if the process dies, so the .bat auto-restart still
    works after a crash. Fails OPEN if locking is unavailable (e.g. non-Windows)."""
    global _lock_fh
    try:
        os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
        _lock_fh = open(LOCK_PATH, "w")
        try:
            import msvcrt
            msvcrt.locking(_lock_fh.fileno(), msvcrt.LK_NBLCK, 1)
        except ImportError:
            import fcntl
            fcntl.flock(_lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False
    except Exception:
        return True  # don't block trading on an unexpected lock error


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}Z] {msg}", flush=True)


async def _held_tickers() -> str:
    from src.clients.kalshi_client import KalshiClient
    c = KalshiClient()
    try:
        r = await c._make_authenticated_request("GET", "/trade-api/v2/portfolio/orders",
                                                params={"limit": 200}, require_auth=True)
        # Dedup must include RESTING/pending buys, not just executed -- otherwise
        # an unfilled limit buy is invisible and the same ticker gets re-bought
        # every tick, over-deploying. (This was a real over-deploy cause.)
        active = ("executed", "resting", "accepted", "pending")
        held = {o["ticker"] for o in r.get("orders", [])
                if o.get("action") == "buy" and o.get("status") in active}
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
        "exit":   ("SOLD", "WOULD SELL", "healthy"),
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
    if live and not can_trade:
        log(f"cash ${cash:.2f}, headroom ${headroom:.2f} -> trading paused (need >=$1 above ${CASH_FLOOR:.0f} floor).")
    # Process in a fixed priority so caps are NETTED against a single running
    # budget: combined deployment this tick can never exceed headroom (= cash -
    # floor), so the floor can't be breached even if every buy fires at once.
    # Soccer gets first claim on the reserve; sports only uses excess above it.
    remaining = headroom
    order = sorted(due, key=lambda n: {"exit": 0, "soccer": 1, "sports": 2}.get(n, 9))
    for name in order:
        cfg = SCHEDULE[name]
        args, env_extra = [], {"RUNNER_SKIP_TICKERS": skip}
        kind = cfg["kind"]
        if kind == "sell" and live:
            # protective exit: always live when the runner is live, no cash gate
            env_extra.update(cfg["live_env"])
        elif kind == "buy" and can_trade:
            if name == "soccer":
                cap = round(min(SOCCER_CAP, remaining), 2)
            else:  # sports must leave the soccer reserve untouched
                cap = round(min(SPORTS_CAP, max(0.0, remaining - SOCCER_RESERVE)), 2)
            if cap < 1.0:
                log(f"{name:8s} -> skipped (only ${remaining:.2f} headroom left this tick)")
                state[name] = now
                _save_state(state)
                continue
            remaining = round(remaining - cap, 2)   # reserve it from the shared budget
            args = ["--live"]
            env_extra["TRADING_HALTED"] = "false"
            env_extra.update(cfg["live_env"])
            env_extra["CONV_MAX_CAPITAL" if name == "sports" else "SOCCER_MAX_CAPITAL"] = str(cap)
        out = _run(cfg["module"], args, env_extra)
        log(f"{name:8s} -> {_summary(name, out)}")
        state[name] = now
        _save_state(state)


async def main() -> None:
    live = "--live" in sys.argv
    if not _acquire_single_instance_lock():
        log("Another runner instance is already running (lock held). Exiting to avoid double-trading.")
        return
    log("=" * 60)
    log(f"AUTONOMOUS RUNNER starting ({'LIVE' if live else 'DRY-RUN'})")
    log("cadence: soccer 90s | sports 3h | crypto 24h | funding 24h")
    log(f"cash floor ${CASH_FLOOR:.0f} | per-strategy caps apply | Ctrl+C to stop")
    log("=" * 60)
    state: dict = _load_state()
    if state:
        ago = {k: f"{(time.time()-v)/60:.0f}m ago" for k, v in state.items()}
        log(f"resumed state (won't re-fire recent runs): {ago}")
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
