#!/usr/bin/env bash
# Continuously runs main.py (dry-run) against a rotating set of live
# sports/esports game markets: refreshes tracked_markets.json to currently-
# active games, runs main.py against that list for REFRESH_SECONDS, then
# restarts against a freshly-refreshed list (main.py only reads the market
# list once at startup, so restarting is how it picks up newly-live games
# as old ones resolve). Logs to $LOG_DIR/main_<cycle>.log per cycle.
set -uo pipefail

cd "$(dirname "$0")/.."

REFRESH_SECONDS="${REFRESH_SECONDS:-900}"
TARGET_COUNT="${TARGET_COUNT:-10}"
LOG_DIR="${LOG_DIR:-/tmp/live_games_loop}"
mkdir -p "$LOG_DIR"

cycle=0
while true; do
  cycle=$((cycle + 1))
  echo "[$(date -u +%FT%TZ)] cycle $cycle: refreshing live game markets"
  uv run python scripts/refresh_live_games.py --target-count "$TARGET_COUNT" --max-refreshes 1 \
    >> "$LOG_DIR/refresh.log" 2>&1

  echo "[$(date -u +%FT%TZ)] cycle $cycle: starting main.py for ${REFRESH_SECONDS}s"
  uv run python main.py > "$LOG_DIR/main_${cycle}.log" 2>&1 &
  main_pid=$!

  sleep "$REFRESH_SECONDS"

  kill "$main_pid" 2>/dev/null
  wait "$main_pid" 2>/dev/null
done
