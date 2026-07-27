# Handover: Polymarket bot — live-game arbitrage validation run

**Written:** 2026-07-27 ~15:14 UTC, after ~20 hours of continuous unattended dry-run operation.
**Purpose:** let a fresh Claude Code session (or a human) pick this up without re-deriving everything below.

## TL;DR

- Goal (from `/goal`): find a strategy that makes >0.5% profit consistently every day.
- After exhaustively ruling out arb/market-making/momentum/news-edge/liquidity-rewards on **political and Fed-decision markets** (10+ independent real-data tests, all negative — see full history in conversation transcript, not repeated here), found a **real, out-of-sample-replicated edge**: `ComplementaryOutcomesSignal` (taker_fee_bps=200, min_edge_bps=10) on **live, in-play sports/esports game markets** (not political markets — those are dead or already-arbitraged; live games have real, fast information events that create temporary genuine mispricings).
- Currently running continuously, unattended, in **dry-run mode** (no real money, `DRY_RUN=true` / `LIVE_TRADING_CONFIRMED=false` in `.env` — **do not flip these without explicit user instruction**).
- **Result so far (~20h): $45–65 realized (fully closed, cash-in-hand) profit on a $2000 bankroll**, ~2–3% for the period. This is from **round-trip trades only** (buy complementary pair cheap, sell back at a profit as game odds shift) — **zero markets have formally resolved yet**, so none of this profit depends on market settlement. It's real, closed, already-realized P&L.
- Sample size is one day; do not treat $45–65/day as a proven daily rate yet. Needs more days, especially since resolution-based settlement (a second profit channel) hasn't kicked in at all yet.

## What's currently running (3 background processes)

```
scripts/run_live_games_loop.sh   # orchestrator: refreshes live game list + restarts main.py every 15 min
  └─ main.py                      # actual trading bot, dry-run, WebSocket-collecting order books + logging decisions
scripts/refresh_all_metadata.py --interval-seconds 300   # NEW (added this session): re-fetches Gamma metadata
                                                           # for EVERY condition_id ever collected, every 5 min,
                                                           # so market resolutions get caught even after a game
                                                           # rotates out of the live tracking list
```

Check they're alive:
```bash
ps aux | grep -E "run_live_games_loop|main.py|refresh_all_metadata" | grep -v grep
tail -10 /tmp/live_games_loop_orchestrator.log
tail -10 /tmp/live_games_loop/refresh_all_metadata.log
```

If `run_live_games_loop.sh` died, restart with:
```bash
nohup bash scripts/run_live_games_loop.sh > /tmp/live_games_loop_orchestrator.log 2>&1 &
disown
```
If `refresh_all_metadata.py` died, restart with:
```bash
nohup uv run python -u scripts/refresh_all_metadata.py --interval-seconds 300 > /tmp/live_games_loop/refresh_all_metadata.log 2>&1 &
disown
```
(`-u` for unbuffered output — without it, prints sit in a buffer and the log looks empty even though it's working.)

## How to check current P&L

```bash
uv run python scripts/report_cumulative_arb_pnl.py --window-hours 2   # fast, always safe
uv run python scripts/report_cumulative_arb_pnl.py --window-hours 8   # usually OK, occasionally slow now
```

**⚠️ IMPORTANT: DO NOT use `--window-hours` above ~8-10 right now.** The DB has grown to **13GB** (`polymarket_data.db`) from continuous high-frequency live-game order book collection. A wide-window backtest replay (e.g. 20h+) re-simulates the entire window from scratch and its memory use scales with data volume — a 20h attempt spiked to **17GB RSS** before it was killed to protect the live trading process. Stick to 2-8h windows for routine checks. If a real full-session total is ever needed, budget real time for it, run it with a generous `timeout` (300s+), and watch `free -h` / `ps aux` while it runs — kill it if RSS climbs past ~15GB.

Output fields:
- `realized_pnl` — **the trustworthy number.** Only increments from (a) completed round-trip trades or (b) actual market settlement. Currently 100% from (a) since zero markets have resolved.
- `unrealized_pnl` — mark-to-market noise on still-open positions, can swing either direction, don't over-trust it (see bug history below).
- Narrow windows (2-3h) show fresh/optimistic numbers (implicitly assume full fresh $2000 capital). Wider windows (8h+) show the **capital-constrained** truth — a single continuous $2000 portfolio where capital gets tied up in open positions until they resolve or round-trip closed. The wide-window number is more realistic; the narrow-window number overstates achievable velocity.

Check for market resolutions (the thing to watch for — once markets start actually closing, capital should free up and the wide-window number should start moving again):
```bash
uv run python -c "
from data.store import DataStore
store = DataStore('polymarket_data.db')
print('closed:', store._conn.execute('SELECT COUNT(*) FROM market_metadata WHERE closed=1').fetchone()[0])
"
```
As of this writing: **0 closed**, out of 92 markets ever tracked, after ~20h. Polymarket's official resolution flag lags real-world game completion significantly (oracle/admin process) — this is expected, not a bug, but it means the capital-recycling question is still open.

## Bugs found and fixed this session (all in git-trackable source, not just this run)

1. **`execution/order_manager.py` — idempotency TTL used real wall-clock time (`time.monotonic()`) even during backtesting**, meaning backtest replay speed (how fast the computer runs the replay) silently affected which fills got deduped as "duplicates" — a real reproducibility bug. Fixed by adding a `clock` parameter (defaults to `time.monotonic` for live use); `backtest/engine.py` now injects a `_SimulatedClock` driven by the replayed event's own timestamp. See `tests/execution/test_order_manager.py::test_resubmission_allowed_after_idempotency_ttl_expires` for the regression test (had to be rewritten to inject the clock directly instead of monkeypatching the module).

2. **`data/store.py` — no WAL mode, no busy-timeout on the SQLite connection.** Caused `sqlite3.OperationalError: database is locked` whenever a report script tried to read while `main.py`'s ingestion loop was writing (which is constantly). Fixed: `PRAGMA journal_mode=WAL` + `timeout=30.0` on connect. This is why `polymarket_data.db-wal` and `-shm` files exist now.

3. **`backtest/engine.py` — mark-to-market trusted a stale/one-sided quote.** `OrderBook.mid_price` falls back to a lone bid-only or ask-only price if only one side of the book is present. Found live: a losing outcome's book went fully empty right as its game ended, and the *last* snapshot before that was an anomalous `ask=0.999` (nonsensical for a token Gamma's own outcome_prices showed at 0.0005) — inflated one position's unrealized P&L from a real ~$27 to a fake $359. Fixed: the engine now only updates `latest_prices` when a book has **both** a bid and an ask (genuine two-sided price); a one-sided or empty book leaves the last trusted price unchanged instead of trusting the lone quote. Verified: 280/280 tests still pass, and the reported number dropped from $359.56 to the correct ~$27.50 after the fix.

4. **(This session, proactively, not yet proven to have fired) `main.py`'s metadata refresh only covers currently-tracked markets** — once a game rotates out of `tracked_markets.json` (as it ends and gets replaced), Gamma metadata for it stops being refreshed, so a real-world resolution happening after that point would be invisible to `backtest/engine.py`'s settlement logic (`infer_resolution` only fires on `market.closed == True`). Built `scripts/refresh_all_metadata.py` to independently re-fetch metadata for every condition_id **ever** collected (not just currently-tracked ones), running every 5 min. This is running now; **the metadata gap it fixes has not yet resulted in any newly-detected resolution**, because (per point above) nothing has resolved yet in Gamma's system regardless.

Run `uv run pytest -q` any time — should show `280 passed`.

## Key files (new/modified this session)

- `signals/complementary_outcomes.py` — the validated strategy (unmodified logic, just proven on a new market category)
- `backtest/engine.py` — clock injection fix + mark-to-market fix (see bugs above)
- `execution/order_manager.py` — clock parameter added
- `data/store.py` — WAL mode + timeout
- `scripts/refresh_live_games.py` — finds currently-live sports/esports game markets (tight organic spreads, hours-not-weeks resolution) via Gamma, writes them to `tracked_markets.json`
- `scripts/run_live_games_loop.sh` — orchestrates refresh_live_games.py + restarts `main.py` every 15 min (main.py only reads the market list once at startup, so restarting is how it picks up newly-live games as old ones end)
- `scripts/refresh_all_metadata.py` — NEW, the metadata-blind-spot fix described above
- `scripts/report_cumulative_arb_pnl.py` — the P&L checker described above
- `scripts/analyze_trade_history.py`, `scripts/analyze_momentum.py`, `scripts/analyze_resolution_convergence.py` — earlier investigation tools (all found no edge on political/Fed markets; kept for reference, not actively used now)
- `backtest/optimize.py`, `backtest/walk_forward.py` — grid-search and walk-forward consistency tools, used throughout

## What actually proved the edge is real (not fabricated)

1. Two independent out-of-sample windows (different games, same fixed params, no re-tuning): Window 1 +$19.27/24 fills, Window 2 (fresh games) +$5.40/10 fills — both positive, both cleared the trustworthy-fill-count threshold.
2. Verified no lookahead bias: checked that fills occurred while markets were still `closed=false` (not using future resolution data).
3. Verified fills were distributed across multiple distinct games/tokens, not concentrated in one lucky trade.
4. Ran continuously for ~20h unattended — didn't crash, kept generating real (if bursty/episodic) profit.
5. Caught and fixed my own reporting bug ($359.56 fake number) before it was reported as real — the investigation held itself to the same scrutiny throughout.

## Honest open questions / what to check next in a fresh session

1. **Has anything resolved yet?** Check `closed` count (command above). If >0, run a wide-window report (carefully, see memory warning) and see whether capital actually recycled into new trades — this is the single most important unanswered question.
2. **Does the pattern hold over a second day/night cycle?** One overnight lull (03:00–08:00 UTC, low game availability) has already been observed and matches expectations. A second cycle would strengthen confidence.
3. **Memory/scale**: the DB is 13GB and growing ~1-2GB/hour. Either add a pruning/archival strategy for old snapshots, or accept that wide-window reports need to move to an incremental/streaming approach instead of full replay-from-scratch. Not yet done.
4. **Still dry-run only.** Real trading credentials are in `.env` but `DRY_RUN=true`/`LIVE_TRADING_CONFIRMED=false` are intentionally untouched. Do not flip these without the user explicitly asking for it — see `config.Settings.require_live_trading_confirmation()`.
5. If asked "should we go live with real money," the honest answer as of this writing is: not yet — wait for at least one realized market resolution to confirm the settlement path works as modeled, and ideally a second full day-night cycle of data.

## Safety notes

- `MAX_PORTFOLIO_EXPOSURE_USD=2000` in `.env` (raised from a $300 default earlier this session, at user's request).
- All three background processes are **dry-run only** — no real orders are ever placed, confirmed throughout by code review of `OrderManager._submit()`.
- If you need to stop everything cleanly:
  ```bash
  pkill -f "run_live_games_loop.sh"
  pkill -f "python main.py"
  pkill -f "refresh_all_metadata.py"
  ```
