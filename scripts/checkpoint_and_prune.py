"""Solves the DB-growth problem found in the live-game validation run:
polymarket_data.db was growing ~1-2GB/hour from continuous order book
collection, and every P&L check (scripts/report_cumulative_arb_pnl.py)
recomputes everything from scratch by replaying raw snapshots - which is
both why wide time windows became memory-heavy (one replay spiked to 17GB
before being killed) AND why we couldn't just delete old data: doing so
would silently erase old trades from every future recomputation.

The fix is checkpointing: persist the strategy's portfolio state (cash,
open positions, realized P&L) to a small JSON file after each run, so the
next run only needs to replay events *since* the last checkpoint,
restoring the portfolio from the saved state instead of starting fresh.
Once a checkpoint is saved, every order book snapshot older than it is
provably no longer needed for any future calculation - realized_pnl and
open positions as of that moment are captured in the checkpoint file
itself - so it's safe to delete.

This runs the SAME validated strategy config (ComplementaryOutcomesSignal,
taker_fee_bps=200, min_edge_bps=10) used throughout the live-game
validation, via the same OrderManager/backtest.engine building blocks - it
is not a separate, unvalidated code path.

Pruning is a plain DELETE (safe, WAL-mode-compatible, doesn't require an
exclusive lock) followed by `PRAGMA wal_checkpoint(TRUNCATE)` to keep the
-wal side file from growing unboundedly. DELETE reclaims space *within* the
db file for reuse (stops further growth) but does not shrink the file on
disk immediately - that requires a full VACUUM, which takes an exclusive
lock for a while on a multi-GB file, so it's deliberately NOT run
automatically here. Run `scripts/vacuum_db.py` (or `sqlite3 polymarket_data.db
'VACUUM;'`) manually during a maintenance window if you want to reclaim
disk space.

Usage:
    uv run python scripts/checkpoint_and_prune.py --interval-seconds 1800
"""

import argparse
import json
import sys
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.store import DataStore  # noqa: E402

from backtest.data_source import HistoricalDataSource  # noqa: E402
from backtest.engine import _SimulatedClock, _process_signal_strategy  # noqa: E402
from backtest.portfolio import Portfolio, Position  # noqa: E402
from backtest.report import Prediction  # noqa: E402
from config import settings  # noqa: E402
from execution.order_manager import OrderManager  # noqa: E402
from execution.risk import RiskLimits  # noqa: E402
from signals.complementary_outcomes import ComplementaryOutcomesSignal  # noqa: E402

DEFAULT_CHECKPOINT_PATH = "arb_checkpoint.json"
PRUNE_SAFETY_MARGIN_HOURS = 1.0  # keep a small buffer past the checkpoint in case of clock skew


def load_checkpoint(path: Path, bootstrap_window_hours: float) -> dict:
    """On the very first run (no checkpoint file yet), replaying from the
    beginning of all collected history would be exactly the expensive
    full-history replay that spiked to 17GB earlier in this project's
    validation run - so the bootstrap starts from a bounded recent window
    instead, not from the dawn of the DB. This means the persistent
    checkpoint's realized_pnl counter starts near-zero and does NOT include
    whatever was realized before the bootstrap window - that earlier total
    is a known, separately-recorded snapshot (see HANDOVER.md), not lost,
    just not carried into this new running counter.
    """
    if not path.exists():
        bootstrap_start = datetime.now(timezone.utc) - timedelta(hours=bootstrap_window_hours)
        return {
            "checkpoint_time": bootstrap_start.isoformat(),
            "cash": settings.max_portfolio_exposure_usd,
            "realized_pnl": 0.0,
            "positions": {},
        }
    return json.loads(path.read_text())


def save_checkpoint(path: Path, portfolio: Portfolio, checkpoint_time: datetime) -> None:
    data = {
        "checkpoint_time": checkpoint_time.isoformat(),
        "cash": portfolio.cash,
        "realized_pnl": portfolio.realized_pnl,
        "positions": {
            token_id: asdict(pos) for token_id, pos in portfolio.positions.items() if pos.shares != 0
        },
    }
    path.write_text(json.dumps(data, indent=2))


def restore_portfolio(checkpoint: dict) -> Portfolio:
    portfolio = Portfolio(initial_cash=settings.max_portfolio_exposure_usd)
    portfolio.cash = checkpoint["cash"]
    portfolio.realized_pnl = checkpoint["realized_pnl"]
    portfolio.positions = {
        token_id: Position(**pos_data) for token_id, pos_data in checkpoint["positions"].items()
    }
    return portfolio


def run_once(checkpoint_path: Path, bootstrap_window_hours: float) -> None:
    checkpoint = load_checkpoint(checkpoint_path, bootstrap_window_hours)
    start = datetime.fromisoformat(checkpoint["checkpoint_time"]) if checkpoint["checkpoint_time"] else None
    portfolio = restore_portfolio(checkpoint)

    store = DataStore(settings.db_path)
    try:
        rows = store._conn.execute("SELECT DISTINCT condition_id FROM order_book_snapshots").fetchall()
        condition_ids = [r[0] for r in rows]

        data_source = HistoricalDataSource(store)
        markets = data_source.load_markets(condition_ids)

        risk_limits = RiskLimits.from_settings(settings)
        sim_clock = _SimulatedClock()
        order_manager = OrderManager(risk_limits=risk_limits, dry_run=True, clock=sim_clock)
        strategy = ComplementaryOutcomesSignal(taker_fee_bps=200, min_edge_bps=10)

        latest_books: dict[str, object] = {}
        predictions: list[Prediction] = []
        new_fills_before = len(portfolio.fills)
        last_event_time = start

        for event in data_source.replay_events(markets, start=start):
            book = event.order_book
            sim_clock.advance(book.received_at)
            latest_books[book.token_id] = book
            last_event_time = book.received_at
            _process_signal_strategy(
                "arb", strategy, event.market, book, latest_books, order_manager, portfolio, predictions
            )

        new_fills = len(portfolio.fills) - new_fills_before
        checkpoint_time = last_event_time or datetime.now(timezone.utc)
        save_checkpoint(checkpoint_path, portfolio, checkpoint_time)

        open_positions = sum(1 for p in portfolio.positions.values() if p.shares != 0)
        print(
            f"[{datetime.now(timezone.utc).isoformat()}] replayed {new_fills} new fill(s), "
            f"checkpoint: cash=${portfolio.cash:.2f} realized_pnl=${portfolio.realized_pnl:.2f} "
            f"open_positions={open_positions} checkpoint_time={checkpoint_time.isoformat()}"
        )

        prune_before = checkpoint_time - timedelta(hours=PRUNE_SAFETY_MARGIN_HOURS)
        cur = store._conn.execute(
            "DELETE FROM order_book_snapshots WHERE received_at < ?", (prune_before.isoformat(),)
        )
        deleted = cur.rowcount
        store._conn.commit()
        store._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        print(f"[{datetime.now(timezone.utc).isoformat()}] pruned {deleted} snapshot(s) older than {prune_before.isoformat()}")
    finally:
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--checkpoint-path", default=DEFAULT_CHECKPOINT_PATH)
    parser.add_argument("--interval-seconds", type=float, default=1800.0)
    parser.add_argument("--max-runs", type=int, default=0, help="0 = run forever")
    parser.add_argument(
        "--bootstrap-window-hours",
        type=float,
        default=2.0,
        help="Only used if no checkpoint file exists yet - bounds the very first (otherwise full-history) replay",
    )
    args = parser.parse_args()

    checkpoint_path = Path(args.checkpoint_path)
    count = 0
    while True:
        try:
            run_once(checkpoint_path, args.bootstrap_window_hours)
        except Exception as exc:  # noqa: BLE001 - keep the loop alive across transient failures
            print(f"[{datetime.now(timezone.utc).isoformat()}] run failed: {exc}")
        count += 1
        if args.max_runs and count >= args.max_runs:
            break
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    main()
