# Handover: Polymarket bot — live-game arbitrage validation run

**Last updated:** 2026-07-28 ~14:20 UTC.
**Purpose:** let a fresh Claude Code session (or a human) pick this up without re-deriving everything below.

## TL;DR

- Goal (from `/goal`): find a strategy that makes >0.5% profit consistently every day.
- After exhaustively ruling out arb/market-making/momentum/news-edge/liquidity-rewards on **political and Fed-decision markets** (10+ independent real-data tests, all negative — see conversation transcript, not repeated here), found a **real, out-of-sample-replicated edge**: `ComplementaryOutcomesSignal` (taker_fee_bps=200, min_edge_bps=10) on **live, in-play sports/esports game markets**.
- Running continuously, unattended, in **dry-run mode** (`DRY_RUN=true` / `LIVE_TRADING_CONFIRMED=false` in `.env` — **do not flip these without explicit user instruction**).
- **Result through ~22h of the first continuous run: $45–98 realized (fully closed, cash-in-hand) profit on a $2000 bankroll**, from round-trip trades only (zero markets had formally resolved in that window).
- **⚠️ Then a real operational incident (see below): almost all of that run's raw order-book history was accidentally deleted** while building a DB-pruning fix, and a persistent checkpoint/pruning system now runs in its place, restarting the *tracked* realized-P&L counter from ~15:41 UTC 2026-07-27 with a clean $0 baseline. The knowledge that the edge is real and was validated is not lost (it's recorded here and in the conversation), but the raw data to re-derive those specific historical numbers is gone.
- Sample size for the NEW checkpoint-tracked total is small (started ~17:05 UTC). Treat everything as still-accumulating evidence, not a settled daily rate.
- **Update (2026-07-29): the long-standing "zero market resolutions" claim was itself a bug**, not a real finding — see the correction under "Check for market resolutions" below. Markets have been resolving all along; this codebase just couldn't see it until now (63 of 311 tracked markets confirmed closed as of the fix).
- **Update (2026-07-29 ~22:00 UTC): the flat 2% fee placeholder is gone.** `ComplementaryOutcomesSignal` now uses Polymarket's real per-market fee schedule (`feeSchedule.{rate,exponent}` from Gamma, `fee = rate * price**exponent * (1-price)**exponent`), confirmed against `help.polymarket.com/en/articles/13364478` and live API responses (sports=0.05, crypto=0.07, finance=0.04). The flat model had the shape backwards for this strategy's typical favorite/longshot pairs - overcharging the favorite leg, undercharging the longshot leg. 303 of 323 tracked markets now have a parsed fee_rate after one refresh cycle. `taker_fee_bps=200` in `main.py`/`checkpoint_and_prune.py`/`report_cumulative_arb_pnl.py` is now only a fallback for markets Gamma hasn't returned a schedule for, not the primary fee model.

## ⚠️ Incident report: accidental data loss during DB-pruning work (2026-07-27 ~16:37–17:00 UTC)

**What happened:** `polymarket_data.db` had grown to 13GB from continuous high-frequency order book collection, making wide-window P&L reports slow/memory-heavy (one 20h-window attempt spiked to 17GB RSS before being killed). Built `scripts/checkpoint_and_prune.py` to fix this properly (see architecture below). Tested it live against the running 13GB database. The first test run timed out mid-`DELETE` and was killed via `timeout 120s`; the assumption that an uncommitted transaction would roll back on kill was wrong (or the delete had already effectively applied) — checking the DB afterward, it had gone from millions of rows / 13GB of *data* down to 71,756 rows spanning only 23 minutes. The file stayed 13GB on disk (DELETE frees space for reuse but doesn't shrink the file — expected), but the historical rows were gone.

**Impact:** the raw order-book history needed to re-derive the ~22-hour run's $45–98 profit figures via backtest replay is gone. The *fact* that the edge was validated (two independent out-of-sample windows, no lookahead, distributed fills, ~22h unattended operation) is unaffected — that evidence is recorded in this file and the full conversation transcript. What's lost is the ability to re-verify those specific historical numbers from scratch.

**Also caused, separately but around the same time:** `main.py` crashed on a transient network error (Gamma API `Connection reset by peer` — the known intermittent connectivity issue with this sandbox, unrelated to the pruning work) right as this was happening, and `run_live_games_loop.sh` had a pre-existing gap where it didn't restart `main.py` if it crashed mid-cycle (only blindly slept the full 15-minute cycle regardless). This meant the bot sat idle until manually caught. **This gap is now fixed** (see below) — a fresh Claude Code session will not need to notice/fix this again.

**Root cause and lesson**: tested a new, destructive (DELETE) operation against the live, valuable, only copy of the data, instead of testing against a backup or a copy first. Should have copied `polymarket_data.db` before the first live test of a pruning script. If you (human or future Claude session) build anything else that deletes data from this DB, **copy the file first**: `cp polymarket_data.db polymarket_data.db.bak`.

**Current status:** fully recovered. All processes healthy, checkpoint-and-prune now runs safely and fast (~8 seconds per cycle, see below), the crash-resilience gap is fixed, tests all pass (280/280).

## What's currently running (4 background processes)

```
scripts/run_live_games_loop.sh              # orchestrator: refreshes live game list + restarts main.py every 15 min;
  └─ main.py                                 # now also restarts main.py immediately if it dies mid-cycle (fixed this session)
scripts/refresh_all_metadata.py --interval-seconds 300      # re-fetches Gamma metadata for every condition_id ever
                                                              # collected, every 5 min, so resolutions are caught even
                                                              # after a market rotates out of the live tracking list
scripts/checkpoint_and_prune.py --interval-seconds 1800 --bootstrap-window-hours 2   # NEW this session (see below):
                                                              # persists portfolio state to arb_checkpoint.json every
                                                              # 30 min and prunes order book snapshots older than the
                                                              # checkpoint - keeps the DB from growing unboundedly
```

**As of 2026-07-30, this box runs the 3 processes under systemd (not nohup)** — installed directly as the `ubuntu` user against `/home/ubuntu/polymarket_bot` (a lighter-weight variant of `deploy/systemd/`'s dedicated-user template, made because this box was already running that way and a dedicated `polymarket` user migration wasn't requested). This was done specifically so the bot survives reboots unattended — this box has rebooted unexpectedly multiple times (one leftover unattended-upgrades reboot, plus at least one manual `sudo shutdown -r now` from earlier debugging), and previously every reboot required a human to notice all 4 processes were dead and restart them by hand.

Unit files live in `/etc/systemd/system/`:
- `polymarket-bot-orchestrator.service` → `scripts/run_live_games_loop.sh` (manages `main.py` internally, same as before)
- `polymarket-bot-refresh-metadata.service` → `scripts/refresh_all_metadata.py --interval-seconds 300`
- `polymarket-bot-checkpoint.service` → `scripts/checkpoint_and_prune.py --interval-seconds 1800 --bootstrap-window-hours 2`

All three have `Restart=always` and are `enabled` (auto-start on boot via `multi-user.target`). They are NOT copies of the files in `deploy/systemd/` in this repo — those are the production template for a dedicated `polymarket` user under `/opt/polymarket-bot` and are unchanged; this box's live `/etc/systemd/system/*.service` files exist only on the box itself, not in git.

Check they're alive:
```bash
sudo systemctl status polymarket-bot-orchestrator.service polymarket-bot-refresh-metadata.service polymarket-bot-checkpoint.service --no-pager
# or just the process tree:
pgrep -af "run_live_games_loop.sh|checkpoint_and_prune.py|main.py|refresh_all_metadata"
```

Logs are in journald now, not `/tmp/live_games_loop/*.log`:
```bash
sudo journalctl -u polymarket-bot-orchestrator.service -n 50 --no-pager
sudo journalctl -u polymarket-bot-refresh-metadata.service -n 50 --no-pager
sudo journalctl -u polymarket-bot-checkpoint.service -n 50 --no-pager
```

Restart commands if any died (systemd should already do this automatically via `Restart=always`, but if the whole unit is stopped/failed):
```bash
sudo systemctl restart polymarket-bot-orchestrator.service
sudo systemctl restart polymarket-bot-refresh-metadata.service
sudo systemctl restart polymarket-bot-checkpoint.service
```

**Watch out for duplicate `main.py` processes**: if you ever manually start `main.py` outside systemd while `polymarket-bot-orchestrator.service` is also managing it, you'll get two instances. Check with `systemctl status polymarket-bot-orchestrator.service` (shows the managed process tree) — kill anything outside that cgroup.

## The checkpoint-and-prune architecture (new this session)

**The problem it solves**: every P&L check previously recomputed everything from scratch by replaying raw order book snapshots through the backtest engine. This is why wide time windows became memory-heavy, AND why we couldn't just delete old data — doing so would silently erase old trades from every future recomputation.

**The fix**: `scripts/checkpoint_and_prune.py` persists the strategy's actual portfolio state (cash, open positions, realized P&L) to `arb_checkpoint.json` after each run. The next run only replays events *since* the last checkpoint, restoring the portfolio from the saved state instead of starting fresh. Once a checkpoint is saved, every order book snapshot older than it is provably no longer needed for any future calculation, so it's safe to prune (`DELETE ... WHERE received_at < checkpoint_time - 1h safety margin`).

**Two bugs found and fixed while building this:**
1. The very first run (no checkpoint yet) would otherwise replay the *entire* history from scratch — exactly the expensive operation that caused problems in the first place. Fixed with a `--bootstrap-window-hours` parameter (default 2h) that bounds the first run.
2. The `DELETE ... WHERE received_at < ?` query had no usable index — the only index on `order_book_snapshots` was a composite `(token_id, received_at)`, which can't be used for a bare `received_at` predicate, causing a full table scan on a 13GB table (this is what caused the timeout that led to the incident above). Fixed by adding `idx_order_book_snapshots_received_at` to the schema in `data/store.py` (an index on `received_at` alone). **This index had to be built once on the existing live DB** (took ~30s) — a fresh DB created from the current schema will have it automatically.

To check the current checkpoint state directly:
```bash
cat arb_checkpoint.json
```
(gitignored — it's runtime state, not source)

## How to check current P&L

```bash
uv run python scripts/report_cumulative_arb_pnl.py --window-hours 2   # fast, always safe
uv run python scripts/report_cumulative_arb_pnl.py --window-hours 8   # should be fast now that pruning keeps the DB small
```

The DB is now kept small by `checkpoint_and_prune.py` (pruned down from 13GB/millions of rows to ~3,600 rows as of this writing), so the earlier memory warnings about wide windows should no longer apply *going forward* — but the underlying `report_cumulative_arb_pnl.py` script still does a full replay-from-scratch each time (it doesn't yet use the checkpoint), so if the DB grows large again before another prune cycle runs, the same caution applies: watch `free -h` / `ps aux` for RSS above ~10-15GB and kill if needed.

Output fields:
- `realized_pnl` — **the trustworthy number.** Only increments from (a) completed round-trip trades or (b) actual market settlement.
- `unrealized_pnl` — mark-to-market noise on still-open positions, can swing either direction.
- Narrow windows (2-3h) show fresh/optimistic numbers (implicitly assume full fresh $2000 capital). Wider windows show the capital-constrained truth (a single continuous $2000 portfolio where capital gets tied up in open positions until they resolve or round-trip closed).

Check for market resolutions:
```bash
uv run python -c "
from data.store import DataStore
store = DataStore('polymarket_data.db')
print('closed:', store._conn.execute('SELECT COUNT(*) FROM market_metadata WHERE closed=1').fetchone()[0])
"
```
**Correction (2026-07-29, ~21:00 UTC):** the "0 closed" reading reported here and in every session before this one was **wrong, and it was our own bug, not Polymarket's oracle lag as previously assumed.** `GammaClient.get_markets_by_condition_ids()` queried `/markets` with only `condition_ids`, no `closed` param — Gamma silently defaults to `closed=false` when that's omitted, so any market that had already resolved was invisible to `scripts/refresh_all_metadata.py` no matter how long it polled. Verified directly against a known-resolved CS:GO market (Imperial vs BESTIA): the exact query this codebase used returned 0 results; adding `closed=true` explicitly returned it with `outcomePrices: ["1","0"]`. Fixed in `data/gamma_client.py` (now queries both `closed=false` and `closed=true`, merges results) — **first refresh after the fix found 63 already-resolved markets out of 311 tracked**, not 0. Re-run the check above; it should now show a nonzero count.

## Bugs found and fixed this session (all in git-trackable source)

1. **`execution/order_manager.py` — idempotency TTL used real wall-clock time (`time.monotonic()`) even during backtesting**, meaning backtest replay speed silently affected which fills got deduped as "duplicates". Fixed with an injectable `clock` parameter; `backtest/engine.py` now injects a `_SimulatedClock` driven by the replayed event's own timestamp.

2. **`data/store.py` — no WAL mode, no busy-timeout on the SQLite connection.** Caused `sqlite3.OperationalError: database is locked` under concurrent read/write. Fixed: `PRAGMA journal_mode=WAL` + `timeout=30.0`.

3. **`backtest/engine.py` — mark-to-market trusted a stale/one-sided quote**, inflating one position's unrealized P&L from a real ~$27 to a fake $359.56 when a losing outcome's book went fully empty at game-end. Fixed: only update `latest_prices` when a book has both a bid and an ask.

4. **`main.py`'s metadata refresh only covers currently-tracked markets** — fixed with `scripts/refresh_all_metadata.py` (see above).

5. **`data/store.py` — missing index on `order_book_snapshots.received_at`** caused a full table scan on any age-based query/delete. Fixed with `idx_order_book_snapshots_received_at` (see checkpoint-and-prune section above). This was the direct cause of the data-loss incident (the slow, unindexed DELETE is what caused the timeout-and-kill).

6. **`scripts/run_live_games_loop.sh` — no crash-resilience within a cycle.** Blindly slept the full `REFRESH_SECONDS` (900s) regardless of whether `main.py` was still alive, so a crash (e.g. transient network error) caused up to 15 minutes of downtime. Fixed: now polls every 10s and restarts `main.py` immediately if it died, without waiting for the next scheduled cycle boundary.

Run `uv run pytest -q` any time — should show `280 passed`.

## Key files (new/modified this session)

- `signals/complementary_outcomes.py` — the validated strategy (unmodified logic, just proven on a new market category)
- `backtest/engine.py` — clock injection fix + mark-to-market fix
- `execution/order_manager.py` — clock parameter added
- `data/store.py` — WAL mode + timeout + new `received_at` index
- `scripts/refresh_live_games.py` — finds currently-live sports/esports game markets via Gamma, writes to `tracked_markets.json`
- `scripts/run_live_games_loop.sh` — orchestrates the rotation + main.py restarts; now with crash-resilience
- `scripts/refresh_all_metadata.py` — metadata-blind-spot fix
- `scripts/checkpoint_and_prune.py` — NEW, the checkpoint/prune architecture described above
- `scripts/report_cumulative_arb_pnl.py` — P&L checker (still does full replay-from-scratch, not yet checkpoint-aware)
- `deploy/systemd/` — systemd unit files + README for VPS deployment (`Restart=always` gives crash/reboot resilience beyond what the nohup dev setup provides)
- `scripts/analyze_trade_history.py`, `scripts/analyze_momentum.py`, `scripts/analyze_resolution_convergence.py` — earlier investigation tools (all found no edge on political/Fed markets; kept for reference)
- `backtest/optimize.py`, `backtest/walk_forward.py` — grid-search and walk-forward consistency tools

## What actually proved the edge is real (not fabricated) — historical record, data since pruned

1. Two independent out-of-sample windows (different games, same fixed params, no re-tuning): Window 1 +$19.27/24 fills, Window 2 (fresh games) +$5.40/10 fills — both positive, both cleared the trustworthy-fill-count threshold.
2. Verified no lookahead bias: fills occurred while markets were still `closed=false`.
3. Verified fills were distributed across multiple distinct games/tokens, not concentrated in one lucky trade.
4. Ran continuously for ~22h unattended before the pruning incident — didn't crash from the strategy itself, kept generating real (if bursty/episodic) profit, accumulating to $45-98 realized depending on exact check timing/window.
5. Caught and fixed a real reporting bug ($359.56 fake number) before it was reported as real.

## Niche/low-volume market pool (added this session, exploratory/unvalidated)

Research review of state-of-the-art prediction-market strategies (see below) found: (a) classic same-market YES/NO arb is now dominated by sub-100ms bots (IMDEA study, $40M extracted from Polymarket Apr'24-Apr'25, 73% to sub-100ms bots); (b) an academic study of 173 Polymarket NBA games (arXiv 2605.00864) found combinatorial arb across *correlated* markets (moneyline/spread/total) produces ~40x more opportunities than single-market arb, concentrated in final minutes of live play — this matches the market category the validated edge already trades, but isn't built yet (would need new cross-market-correlation signal logic); (c) low-volume/niche markets reportedly keep persistent gaps longer since fewer bots compete them away.

Acted on (c), the cheapest test: `scripts/refresh_live_games.py` now also tracks a second pool via `fetch_niche_markets()` — active, order-book-enabled markets with 24h volume in [200, 20000] (below the sports pool's 50k floor), excluding anything matching the sports "vs."/"Winner" pattern. Controlled by `--niche-count` (orchestrator: `NICHE_COUNT` env var, default 5 in `run_live_games_loop.sh`). Uses the exact same validated `ComplementaryOutcomesSignal` — only market *selection* changed, no new strategy code. **Not yet validated** — this is a live experiment, not a proven edge like the sports pool. `market_metadata.category` is always NULL from the current Gamma client mapping, so there's no clean way yet to separate niche-pool P&L from sports-pool P&L in the aggregate report — treat `report_cumulative_arb_pnl.py`'s number as combined until/unless that's built.

Also checked (this session): `MarketMakingStrategy` has been running live in dry-run this whole time (unconditionally in `main.py`'s `build_strategies()`) but was never measured on sports markets — only ruled out on political/Fed markets. Added `scripts/report_market_making_pnl.py` to check it; first read: 2 fills, $0 realized, ~-$1.5 to -$1.83 unrealized over 4-24h — inconclusive, tiny sample, not obviously an edge.

Restarted the orchestrator this session to pick up `--niche-count`; PID changed (old 983796 → new one, check `pgrep -af run_live_games_loop.sh`).

## Fee accounting and book-depth sizing fixes (2026-07-27 ~21:00 UTC)

User asked whether fees were being considered in P&L, and whether trade size considered available book depth. Neither was true until this fix:

1. **Fees were never deducted anywhere.** `ComplementaryOutcomesSignal` required edge to clear an estimated 2% taker fee before firing, but that fee was only ever used as a *threshold gate* — `Portfolio.apply_fill` moved cash at the raw fill price with no fee subtracted. All P&L numbers reported before this fix (including the $5.83/$45-98 figures) were **gross, not net, of fees**. Fixed: `apply_fill` now takes `fee_rate`, folded into an effective price so it hits cash immediately and cost-basis/realized_pnl on close; `backtest/engine.py` threads `strategy.taker_fee_rate` through.
2. **Order sizing ignored book depth.** `handle_multi_leg_signal` sized arb baskets purely from the risk cap (`max_order_usd / basket_cost`), never checking how many shares were actually resting at the quoted best bid/ask. Fixed: `ComplementaryOutcomesSignal` now reports `max_shares` (thinnest leg's available size) in signal metadata; `OrderManager` caps the basket to it.

Confirmed both are live: `report_cumulative_arb_pnl.py --show-fills` now shows trade sizes capped to real book depth (5-19 shares instead of 100+) and correspondingly smaller, more honest realized_pnl. **Restarted all 3 background processes** (`run_live_games_loop.sh`, `checkpoint_and_prune.py`) to pick up the fix — `refresh_all_metadata.py` untouched (doesn't do portfolio accounting). PIDs changed again, check `pgrep -af run_live_games_loop.sh` / `pgrep -af checkpoint_and_prune.py`.

**Implication**: every P&L figure quoted anywhere before this timestamp (including HANDOVER's historical $45-98 record and this session's $5.83/$6.00 checkpoint reads) was optimistic — real fees and real depth constraints were not applied. Treat this as the point the numbers became trustworthy, not before.

## 48h checkpoint: optimization findings (2026-07-28 ~14:00-14:20 UTC)

Cumulative realized_pnl reached **$116.83** on the corrected (fee/depth-aware) accounting before the next fixes below, from $2000 starting capital, over roughly a day of active post-fix operation. Two real findings from asking "how do we optimize":

1. **Capital, not signal availability, is now the binding constraint.** By ~13:37 UTC cash had gone slightly negative (-$3.31, see the sizing-cap fix below) with **28 open positions** absorbing nearly the entire $2000. Checked all 14 distinct markets behind those positions: 10 had already finished in real life but Polymarket still showed `closed=false` up to **15+ hours** after the scheduled end time, confirming Polymarket's resolution/oracle process is a real, unpredictable bottleneck outside the bot's control. The strategy can't open new positions once capital is fully committed, regardless of how much edge is available — this is the actual throughput ceiling right now, not detection quality.
2. **`market_making` was net-negative operational cost with no proven edge.** `scripts/report_market_making_pnl.py` showed inconclusive/slightly negative P&L (-$1.5 to -$1.83 unrealized) on a tiny sample. Separately, `decisions_log` (no pruning, unlike `order_book_snapshots`) had grown to **9.55M rows**, and a group-by showed **9,502,569 of them (99.5%) were rejected `market_making` orders** vs. only 1,240 total for the validated `complementary_outcomes` strategy. This alone was enough to make `scripts/cli.py`'s journal replay time out. **Fixed: `market_making` is now off by default** (`ENABLE_MARKET_MAKING=true` in `.env` to re-enable) - see `main.py build_strategies()`. Also deleted the 9.51M stale rejected rows from the live DB as a one-time cleanup.

Also fixed this session: the arb basket's exposure-cap sizing didn't reserve room for the taker fee, so cash could run slightly negative once the portfolio was near-fully deployed (caught live: -$3.31). Fixed in `execution/order_manager.py`'s `handle_multi_leg_signal` by including `signal.metadata["fee_cost"]` in the per-set cost used for sizing.

**Real remaining optimization levers:**
- Attempted to backtest raising `MAX_PORTFOLIO_EXPOSURE_USD` and lowering `min_edge_bps` before changing anything live - both came back inconclusive because the retained data window (`checkpoint_and_prune.py`'s `PRUNE_SAFETY_MARGIN_HOURS=1.0` keeps only ~1-1.5h of raw order book history at any time) was too thin: zero fills at the real 200bps-fee config in a 65-minute/33-market sample. Backtesting parameter changes isn't currently viable without either widening that retention window first (costs disk, costs waiting time) or just testing live in dry-run.
- **Decision made 2026-07-28 ~14:35 UTC: raised `MAX_PORTFOLIO_EXPOSURE_USD` from $2000 to $4000 live (dry-run, user-approved), watching real results** rather than waiting on a backtest that data thinness makes untrustworthy anyway. Restarted all 3 background processes to pick it up.
- Lowering `min_edge_bps` (currently 10, hardcoded via `ComplementaryOutcomesSignal(taker_fee_bps=200, min_edge_bps=10)` in both `main.py`'s default construction and `report_cumulative_arb_pnl.py`) to trade smaller per-trade profit for faster capital turnover is still untested - same data-thinness problem applies. Not changed.
- **Found and fixed a second, deeper negative-cash bug (2026-07-28 ~14:51 UTC):** the earlier sizing fix (reserve fee room when sizing a single trade) wasn't enough - cash got *worse*, not better, after it (-$3.31 -> -$33.38), because `OrderManager._record_exposure` (called from `_submit`) still recorded raw `price*size` with no fee, so the exposure ledger never reflected cumulative fees paid across the session, letting real cash drift further from the tracked "committed capital" every trade. Fixed at the root: `_submit` now takes `fee_rate` and books `price*size*(1+fee_rate)`; `handle_multi_leg_signal` recovers the rate from its own sizing calc and passes it per leg. Restarted all 3 processes again to pick this up.
- No lever exists to speed up Polymarket's own resolution process - that bottleneck is structural, not something this codebase can fix.

## Market selection widened (2026-07-28 ~16:00 UTC)

Found live: standalone `Spread: Team (-1.5)` markets (e.g. `Spread: Cincinnati Reds (-1.5)`, 27k 24h volume) don't contain "vs"/"Winner" and exceed the niche pool's $20k ceiling - invisible to both pools despite being legitimate, tradeable live-game markets. Added `Spread:`/`Moneyline:`/`Total:` to the sports-pool matching keywords (shared via `_is_game_market()` in `scripts/refresh_live_games.py`, also used by the niche pool's exclusion check so the two pools still don't double-count). Also bumped default pool sizes (`target_count` 10->15, `niche_count` 5->8) now that `MAX_PORTFOLIO_EXPOSURE_USD` is $4000 instead of $2000. Restarted the orchestrator (not `checkpoint_and_prune.py`, which doesn't touch market selection).

## Real root cause of the negative-cash saga (2026-07-28 ~16:35 UTC)

Cash kept getting worse (-$3.31 -> -$33.38 -> -$92.76) even after the fee-exposure-ledger fix earlier. Turned out the fee fix was correct but wasn't the (whole) problem: `scripts/checkpoint_and_prune.py` hardcoded `DEFAULT_INITIAL_CASH=2000`, completely independent of `settings.max_portfolio_exposure_usd`. When that cap was raised to $4000 (this session, ~14:35 UTC), the risk gate started approving up to $4000 of committed notional while the simulated portfolio only ever had $2000 backing it - negative cash was mathematically guaranteed past $2000 committed, with or without the fee bug. Same class of issue existed independently in `report_cumulative_arb_pnl.py`/`report_market_making_pnl.py` (never passed `initial_cash` to `run_backtest()`, silently defaulted to `backtest/engine.py`'s $1000) and `scripts/cli.py`'s `--initial-cash` default (hardcoded $1000).

**Fixed**: all four now use `settings.max_portfolio_exposure_usd` as the single source of truth, so this can't silently desync again if the cap changes. Also did a one-time +$2000 correction to `arb_checkpoint.json`'s cash to match today's cap increase (cash: -92.76 -> 1907.24; realized_pnl/positions untouched). Restarted `checkpoint_and_prune.py` to pick this up.

**Lesson**: when changing a config value that other scripts assume/hardcode independently, grep for every place that value (or an equivalent hardcoded default) is used before declaring the change complete.

## Live-funds awareness added (2026-07-30) — closes a real pre-live-trading gap

Investigated whether the bot ever checks real available funds before sizing
a live order. It didn't: `MAX_PORTFOLIO_EXPOSURE_USD` (bankroll used for
Kelly sizing and the portfolio exposure cap) was one static `.env` number
used identically for paper AND live trading, with zero connection to the
actual wallet balance - `Portfolio.cash` (used for P&L reporting) is a pure
backtest ledger never wired into the live order path, and `py-clob-client`'s
`get_balance_allowance()` existed but was never called anywhere. A live
order would have been sized against a config number with no idea whether
the wallet actually held that much, surfacing only as a reactive "order
rejected" from the exchange after the fact.

Fixed:
- **Split the bankroll cap in two**: `MAX_PORTFOLIO_EXPOSURE_USD` now only
  applies when `DRY_RUN=true`; a new `LIVE_MAX_FUND_USD` (default 300,
  currently in `.env` at 300 and still inert since `DRY_RUN=true`) applies
  when `DRY_RUN=false`. Wired via `RiskLimits.from_settings(settings,
  portfolio_exposure_usd_override=...)` in `main.py`'s `build_order_manager()`.
- **Real live-balance check**: `execution/client.py`'s new
  `get_collateral_balance_usd(client)` queries Polymarket's
  `/balance-allowance` endpoint (USDC, 6-decimal base units). `OrderManager`
  takes an optional `live_balance_fn`, wired to this only in live mode.
  Every risk-gate check now caps the effective portfolio exposure ceiling to
  `min(LIVE_MAX_FUND_USD, live_balance)` (30s cache, keyed off the same
  clock used for idempotency; a failed fetch falls back to the last known
  value rather than blocking trading, logged loudly either way). See
  `OrderManager._effective_risk_limits()`.
- **`RiskLimits.max_portfolio_exposure_usd` validation relaxed** from
  "must be positive" to "must be non-negative" - a live wallet balance of
  exactly $0 needs to be representable as a valid state (no room, order
  rejected via the normal check_order() path), not a construction error.
  `max_position_usd`/`max_order_usd` are unaffected, still strictly positive.
- Still **dry-run only** (`DRY_RUN=true`) - this is inert on the currently
  running bot, but is now in place before live trading is ever considered.
  22 new/updated tests across `tests/execution/test_risk.py`,
  `test_order_manager.py`, `test_client.py`, `tests/test_config.py`.

## First live canary test, and the py-clob-client v1->v2 migration (2026-07-31)

**First-ever live attempt** (13:44-14:02 UTC, `LIVE_MAX_FUND_USD=30`,
`MAX_ORDER_USD=10`, `MAX_POSITION_USD=15`, `MIN_EDGE_BPS=100`): before
flipping live, ran `get_collateral_balance_usd()` against the real wallet for
the first time ever and found `CLOB_SIGNATURE_TYPE=0` (EOA) was wrong - the
account actually uses signature type 2 (Polymarket browser proxy wallet).
Testing all three signature types found the real $260.46 balance (matching
polymarket.com) only under type 2. Fixed in `.env`. Real bug, would have
broken live order signing entirely if not caught first.

**Then, once live**: every single order failed with `PolyApiException[400]:
"invalid order version, please use the latest clob-client"` - 72 FAILED
orders, 0 successful, $0 lost (orders rejected before any funds moved,
caught by the 10-minute anomaly-monitoring cron and reverted automatically).
Root cause: Polymarket migrated the CLOB to V2 on 2026-04-28 (new order
struct, EIP-712 exchange domain version 1->2) and archived the original
`py-clob-client` package on 2026-05-11. Every v1-signed order is rejected -
this affects the *entire* installed base of the old client, not just this
project. See https://docs.polymarket.com/v2-migration and
https://github.com/Polymarket/py-clob-client/issues/337.

**Fixed**: migrated `execution/client.py`, `execution/orders.py`,
`scripts/generate_api_creds.py` (+ their tests) from `py-clob-client` to
`py-clob-client-v2`. Verified against the installed package's actual source
(not just the migration docs, which had some inaccuracies - e.g. claimed the
`ClobClient` constructor became dict-based; it didn't, still positional args)
before writing any code:
- `ClobClient.__init__`, `OrderArgs`, `get_balance_allowance`,
  `BalanceAllowanceParams`, `AssetType`, `create_order`/`post_order` are all
  backward-compatible - only the import path changes.
- `client.cancel(order_id)` (v1) -> `client.cancel_order(OrderPayload(orderID=order_id))`
  (v2) - a real breaking change, only usage was `execution/orders.py`'s
  `cancel_order()` wrapper (not called anywhere live yet).
- `client.create_or_derive_api_creds()` (v1) -> `client.create_or_derive_api_key()`
  (v2), used only by the one-time `scripts/generate_api_creds.py` setup script.
- Confirmed post-V2 collateral is **pUSD, not USDC.e** (a separate change in
  the same migration - API-only traders may need to `wrap()` USDC.e into pUSD
  via the Collateral Onramp contract first) - pUSD is also 6 decimals so the
  `/1_000_000` conversion in `get_collateral_balance_usd()` is unchanged. A
  fresh balance check against the v2 client showed the same $260.46 with no
  wrapping needed - this wallet's collateral was already usable.
- Considered migrating to `Polymarket/py-sdk` (a newer unified REST+WebSocket
  SDK the py-clob-client-v2 README recommends for new projects) instead, but
  it's pre-1.0, explicitly warns of breaking changes between minor versions,
  and isn't a drop-in replacement (different architecture entirely - would
  mean also rewriting `data/gamma_client.py`/`data/ws_client.py`). Deferred as
  a separate, deliberate future decision - out of scope for fixing what broke
  the canary test. `py-clob-client-v2` itself is not archived/deprecated,
  only "recommended against for new projects" in favor of the unified SDK.
- All 301 tests pass. `pyproject.toml`/`uv.lock` updated (`py-clob-client`
  removed, `py-clob-client-v2==1.1.0` added).

**Not yet verified**: actual order *placement* end-to-end with the new
client - the balance check is read-only. A second, smaller/shorter live
canary test is the next step to confirm the fix actually works before
trusting it for anything longer.

## Second live canary test: v1->v2 fix confirmed, then a NEW hard blocker found (2026-07-31)

Ran the second, tighter canary test (14:27 UTC, same conservative params:
`LIVE_MAX_FUND_USD=30`, `MAX_ORDER_USD=10`, `MAX_POSITION_USD=15`,
`MIN_EDGE_BPS=100`), monitored every 5 min. First ~50 minutes: 0 orders
either way (real markets don't guarantee a qualifying signal on any
schedule - not a problem). Then 2 orders failed with a **completely
different** error than the first canary test:
```
PolyApiException[status_code=403]: "Trading restricted in your region,
please refer to available regions - https://docs.polymarket.com/developers/CLOB/geoblock"
```
This confirms **the py-clob-client-v2 migration itself worked** - this error
happens at a different, later layer than the order-version check (the exact
thing that migration fixed). $0 lost, balance unchanged at $260.46, reverted
immediately per the monitoring job's instructions.

**Root cause: this box's public IP geolocates to London, UK** (AWS
`eu-west-2`). Confirmed via `curl -s https://ipinfo.io/json` (`"city":
"London", "country": "GB"`), then confirmed against Polymarket's own
[geoblock docs](https://docs.polymarket.com/developers/CLOB/geoblock): the
**United Kingdom is on the "Close-Only" restricted list** - new positions
blocked on the frontend AND the API, for every account connecting from a UK
IP, regardless of credentials or account status. This is structural and
permanent for this server's location, not intermittent - every single live
order from this box will be rejected until either the box moves to a
non-restricted region or the restriction changes on Polymarket's end.

**Investigated whether this could be checked via API before starting live
trading** (user request). Two account-level checks both give false
negatives for this exact failure mode:
- `client.get_balance_allowance()` - shows the real balance ($260.46),
  irrelevant to region.
- `client.get_closed_only_mode()` (`GET /auth/ban-status/closed-only`) -
  returned `{"closed_only": False}` the entire time real orders were being
  rejected by the region block. This reflects an account-level compliance
  flag, not the live per-request IP check.

**Found the actual right check** (user pointed to it):
`https://polymarket.com/api/geoblock` - a public, **unauthenticated**,
real-time endpoint that directly answers the live IP-based question:
`{"blocked": true, "ip": "...", "country": "GB", "region": "ENG"}` for this
box, confirmed correct against curl and against the Python wrapper alike.

**Fixed, in two layers**:
1. `execution/client.py`'s new `check_geoblock()` calls this endpoint.
   `main.py`'s `build_order_manager()` now calls it (plus, secondarily,
   `get_closed_only_mode()` for the account-ban case it *does* cover)
   before ever starting live trading, and refuses to start
   (`SystemExit`) if blocked. This is the primary defense - it would have
   caught this exact incident before a single live order was attempted.
2. `execution/orders.py`'s new `GeoRestrictedError` (subclass of
   `OrderPlacementError`) is raised specifically for the 403
   "restricted in your region"/"geoblock" error signature.
   `OrderManager` now has a circuit breaker (`_geo_restricted` flag,
   `geo_restricted` property): the first time this error is seen, all
   further live order attempts short-circuit immediately (no network call)
   instead of repeating the same doomed request. This is the second layer,
   for the case the startup check can't cover: a block starting mid-session.
   Directly motivated by the FIRST canary test's incident (72 identical
   failed orders before a human noticed) - this makes that pattern
   impossible to repeat, for any error class that gets mapped to
   `GeoRestrictedError` in the future.
3. 11 new tests across `tests/execution/test_orders.py`,
   `test_order_manager.py`, `test_client.py`, `tests/test_main.py`.
   312 total tests pass.

**Current status**: live trading is blocked from this specific server,
structurally, until the hosting location changes. Not something further
code changes can fix - see the "Honest open questions" section below for
what to actually do about it.

## Database corruption, found while prepping a snapshot for the eu-west-1 move (2026-07-31)

Plan was to snapshot this Lightsail instance as-is and relaunch it in
eu-west-1 (Ireland) - unlike the UK, Ireland's close-only restriction is
frontend-only per Polymarket's docs, so the API should be unaffected there
(confirm with `check_geoblock()` from the new instance before trusting this).
Before snapshotting, stopped all 3 services and ran `PRAGMA
wal_checkpoint(TRUNCATE)` to avoid shipping a bloated WAL - the WAL had grown
to **35GB** (worse than the earlier disk-full incident), and disk dropped
from 82% to 28% used once truncated.

**Found real, pre-existing database corruption** while checking on this
(unrelated to anything done this session): `PRAGMA integrity_check` reported
18 issues - a genuine B-tree structural problem (`Rowid ... out of order`,
duplicate page references) plus NULL/type corruption and index mismatches in
`market_metadata` and `order_book_snapshots`. Root cause, from
`journalctl`: `polymarket-bot-checkpoint.service` hit `database disk image is
malformed` at 18:06 UTC, then got **OOM-killed** at 19:51 after 1h44m of CPU
time seemingly stuck retrying against the already-corrupt file - the OOM
likely interrupted a write mid-flight and caused (or worsened) the
corruption in the first place.

**decisions_log and activity_log showed zero corruption** - the actual
trade-history evidence for the live-viability question was untouched.
Everything corrupted was disposable: `market_metadata` is a pure Gamma-API
cache, `order_book_snapshots` is raw tick data already superseded by the
checkpoint. Confirmed with the user this data wasn't worth a complex
recovery effort before proceeding.

**Fix**: `DROP TABLE order_book_snapshots` succeeded, but `DROP TABLE
market_metadata` itself failed with the same malformed-image error - the
corruption was bad enough to block DDL on that table, not just reads of
specific rows. Worked around it instead of fighting the corrupted file
further: built a fresh `polymarket_data.db` with the identical schema
(captured from `sqlite_master` first), copied `decisions_log` (2,786 rows)
and `activity_log` (1,819 rows) over via `ATTACH DATABASE ... old`, left
`market_metadata`/`order_book_snapshots` empty, verified `PRAGMA
integrity_check` reports `ok` on the new file, then swapped it into place.
The corrupted original is kept at `polymarket_data.db.corrupted` (also a
redundant full backup at `polymarket_data.db.pre-corruption-fix.bak` - safe
to delete one of these, they're identical, ~7.9GB each) in case anything
needs forensics later. Checkpoint replay against the new DB matched exactly
(`cash=$2257.31 realized_pnl=$545.63 open_positions=36`) - no data loss on
anything that mattered.

**Not yet done**: the actual snapshot-and-move to eu-west-1 - this
corruption discovery interrupted that plan. `market_metadata` will
repopulate automatically as the bot runs; consider waiting for it to refill
a reasonable amount before taking the snapshot, or take it now and let the
new instance repopulate it there instead.

## Honest open questions / what to check next

1. **Has anything resolved yet, and does settlement work? Yes to both, confirmed 2026-07-29.** 63 of 311 tracked markets had resolved (Gamma-client fix above). `checkpoint_and_prune.py` never called `Portfolio.settle()` though - a resolved position just sat open forever at its pre-resolution avg_cost. Fixed: it now cross-references held condition_ids against `market_metadata.closed` and settles via `outcome_prices`. First live run after the fix settled **28 positions across 14 resolved markets in one pass**: cash $2493.99 -> $3893.24, realized_pnl $85.66 -> $140.69, open positions 34 -> 8 legs. This is the strongest validation yet - the core thesis (buy a $1 guaranteed payout for less than $1) held at actual settlement, not just round-trip price action. Remaining open question: keep watching over a longer window to see this repeat, rather than trusting one large batch.
2. **Does the pattern hold over a full second day/night cycle, now tracked via the checkpoint?** The checkpoint restarted at ~17:05 UTC 2026-07-27 with $0 — treat everything from here as a fresh, small sample.
3. **DB growth**: now handled automatically by `checkpoint_and_prune.py` every 30 min. Consider running a manual `VACUUM` during a maintenance window to reclaim the 13GB already allocated on disk (DELETE frees space for reuse but doesn't shrink the file) — not urgent, 476GB free as of this writing.
4. **Still dry-run only.** Do not flip `DRY_RUN`/`LIVE_TRADING_CONFIRMED` without the user explicitly asking — see `config.Settings.require_live_trading_confirmation()`.
5. If asked "should we go live with real money": not yet — wait for at least one realized market resolution to confirm the settlement path works as modeled, and a full day-night cycle of the new checkpoint-tracked data.
6. **Before testing any new destructive/DB-modifying script against the live database, copy it first**: `cp polymarket_data.db polymarket_data.db.bak`. This was not done before testing `checkpoint_and_prune.py` and caused the incident described above.
7. **Live trading is structurally blocked from this server (UK/AWS eu-west-2) - unresolved, not a code problem.** Confirmed via `execution.client.check_geoblock()` (which is now checked automatically before any live start). Options, not yet decided: (a) move the whole deployment to a non-restricted region - the clean fix, but a real migration; (b) route through a VPN/proxy in an allowed region - flagged as risky beyond the technical question, likely violates Polymarket's ToS regardless of feasibility, could get the account banned; (c) leave live trading off entirely and treat this as a paper-trading-only validation project. Do not attempt (b) without the user explicitly weighing that risk first.

## Safety notes

- `MAX_PORTFOLIO_EXPOSURE_USD=2000` in `.env` (raised from a $300 default earlier this session, at user's request).
- All background processes are **dry-run only** — no real orders are ever placed, confirmed by code review of `OrderManager._submit()`.
- A remote (`origin` on GitHub, `git@github.com:crthilakraj/polymarket_bot.git`) is configured but nothing has been pushed — only local commits so far.
- If you need to stop everything cleanly:
  ```bash
  pkill -f "run_live_games_loop.sh"
  pkill -f "python main.py"
  pkill -f "refresh_all_metadata.py"
  pkill -f "checkpoint_and_prune.py"
  ```
