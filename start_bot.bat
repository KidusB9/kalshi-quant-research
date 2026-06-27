@echo off
REM ============================================================
REM  Kalshi Autonomous Trading Bot - persistent live launcher
REM  Strategies: soccer (every 90s), sports (every 3h),
REM              crypto + funding monitor (daily)
REM  Runs forever and auto-restarts if it ever crashes.
REM  Close this window (or Ctrl+C) to stop.
REM
REM  For SAFE dry-run (no real orders), remove "--live" below.
REM ============================================================
setlocal
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

echo ============================================================
echo  KALSHI AUTONOMOUS BOT  (LIVE)
echo  soccer 90s ^| sports 3h ^| crypto + funding daily
echo  Bounded: $10/run cap, $5 cash floor, dedups holdings
echo  Close window or Ctrl+C to stop.
echo ============================================================
echo.

:loop
python -u -m src.autonomous_runner --live
echo.
echo [%date% %time%] Runner exited. Restarting in 15s (Ctrl+C to stop)...
timeout /t 15 /nobreak >nul
goto loop
