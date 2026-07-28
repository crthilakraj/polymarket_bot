"""CLI for inspecting a running (or previously run) bot: current simulated
positions, recent signals/quotes, per-trade P&L, and aggregate P&L. Reads
from the same DecisionJournal and DataStore that main.py writes to at
DB_PATH - works whether main.py is currently running or not, since
everything is persisted.

Usage:
    uv run python scripts/cli.py positions      # current holdings (open positions)
    uv run python scripts/cli.py signals --limit 10   # recent signals/quotes (not necessarily filled)
    uv run python scripts/cli.py trades --limit 20    # every fill ("order"), with profit per closing trade
    uv run python scripts/cli.py pnl             # aggregate P&L

Note: in dry-run mode (the default), "positions"/"trades"/"pnl" are
*simulated* - every SUBMITTED/DRY_RUN decision is treated as an immediate
fill at its quoted price (the same simplification backtest/ uses), because
there's no live fill feed wired up yet (see README Status). In live mode
this will overstate fills for any resting order that never actually got hit.
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.portfolio import Portfolio
from config import settings
from data.store import DataStore
from execution.journal import DecisionJournal
from signals.base import Side

DEFAULT_INITIAL_CASH = 1000.0
DEFAULT_WINDOW_HOURS = 8.0

FILLED_STATUSES = ("SUBMITTED", "DRY_RUN")

# Matches ComplementaryOutcomesSignal's default taker_fee_bps (see
# signals/complementary_outcomes.py) - fees are only actually deducted for
# that strategy's fills (tagged "complementary_outcomes" via main.py's
# build_strategies() dict key), same as report_cumulative_arb_pnl.py, so the
# numbers here are apples-to-apples with that script.
ARB_STRATEGY_NAME = "complementary_outcomes"
ARB_TAKER_FEE_RATE = 200.0 / 10_000


def _replay_portfolio(
    journal: DecisionJournal, initial_cash: float, window_hours: float | None = None
) -> Portfolio:
    """window_hours=None replays every decision ever logged (all_decisions())
    - correct, but can be a very slow full-table scan once decisions_log has
    grown large (hit 9.5M rows/multi-minute in real operation, since that
    table has no pruning unlike data.store.DataStore's order_book_snapshots).
    Pass a window to bound it for interactive use; a position opened before
    the window won't have its cost basis included."""
    portfolio = Portfolio(initial_cash)
    records = (
        journal.all_decisions()
        if window_hours is None
        else journal.decisions_since(datetime.now(timezone.utc) - timedelta(hours=window_hours))
    )
    for record in records:
        if record.status not in FILLED_STATUSES:
            continue
        if record.token_id is None or record.side is None or record.price is None or record.size is None:
            continue
        fee_rate = ARB_TAKER_FEE_RATE if record.strategy == ARB_STRATEGY_NAME else 0.0
        portfolio.apply_fill(
            token_id=record.token_id,
            condition_id=record.condition_id,
            side=Side(record.side),
            price=record.price,
            size=record.size,
            timestamp=record.timestamp,
            strategy=record.strategy,
            fee_rate=fee_rate,
        )
    return portfolio


def cmd_positions(args: argparse.Namespace) -> None:
    journal = DecisionJournal(args.db_path)
    store = DataStore(args.db_path)
    try:
        portfolio = _replay_portfolio(journal, args.initial_cash, args.window_hours)
        open_positions = {tid: pos for tid, pos in portfolio.positions.items() if pos.shares != 0}
        if not open_positions:
            print("No open positions.")
            return

        print(f"{'token_id':<24} {'condition_id':<24} {'shares':>12} {'avg_cost':>10} {'mid':>10} {'value':>12}")
        for token_id, position in open_positions.items():
            book = store.get_latest_order_book(token_id)
            mid = book.mid_price if book else None
            value = position.shares * (mid if mid is not None else position.avg_cost)
            mid_str = f"{mid:.4f}" if mid is not None else "n/a"
            print(
                f"{token_id:<24} {position.condition_id or '':<24} {position.shares:>12.2f} "
                f"{position.avg_cost:>10.4f} {mid_str:>10} {value:>12.2f}"
            )
    finally:
        journal.close()
        store.close()


def cmd_signals(args: argparse.Namespace) -> None:
    journal = DecisionJournal(args.db_path)
    try:
        records = journal.recent_activity(limit=args.limit)
        if not records:
            print("No activity logged yet.")
            return
        for record in records:
            if record.kind == "signal":
                print(
                    f"{record.timestamp.isoformat()}  [{record.strategy}] SIGNAL "
                    f"{record.side} {record.token_id} edge={record.edge_estimate:.4f} "
                    f"confidence={record.confidence:.2f} (market={record.condition_id})"
                )
            else:
                bid = (
                    f"{record.bid_price:.4f}x{record.bid_size:.2f}"
                    if record.bid_price is not None
                    else "-"
                )
                ask = (
                    f"{record.ask_price:.4f}x{record.ask_size:.2f}"
                    if record.ask_price is not None
                    else "-"
                )
                print(
                    f"{record.timestamp.isoformat()}  [{record.strategy}] QUOTE "
                    f"{record.token_id} bid={bid} ask={ask} (market={record.condition_id})"
                )
    finally:
        journal.close()


def cmd_trades(args: argparse.Namespace) -> None:
    """Every fill in order, with each token's own realized/unrealized P&L
    contribution - not just the aggregate. Positive close_pnl means that
    specific closing fill locked in profit; blank means it's still open
    (only visible in cmd_positions' unrealized column)."""
    journal = DecisionJournal(args.db_path)
    store = DataStore(args.db_path)
    try:
        portfolio = Portfolio(args.initial_cash)
        start = datetime.now(timezone.utc) - timedelta(hours=args.window_hours)
        questions: dict[str, str] = {}
        rows = []
        for record in journal.decisions_since(start):
            if record.status not in FILLED_STATUSES:
                continue
            if record.token_id is None or record.side is None or record.price is None or record.size is None:
                continue
            fee_rate = ARB_TAKER_FEE_RATE if record.strategy == ARB_STRATEGY_NAME else 0.0
            realized_before = portfolio.realized_pnl
            portfolio.apply_fill(
                token_id=record.token_id,
                condition_id=record.condition_id,
                side=Side(record.side),
                price=record.price,
                size=record.size,
                timestamp=record.timestamp,
                strategy=record.strategy,
                fee_rate=fee_rate,
            )
            close_pnl = portfolio.realized_pnl - realized_before
            if record.condition_id and record.condition_id not in questions:
                meta = store.get_market_metadata(record.condition_id)
                questions[record.condition_id] = meta.question if meta else record.condition_id
            rows.append((record, close_pnl))

        if not rows:
            print("No trades logged yet.")
            return

        if args.limit:
            rows = rows[-args.limit :]

        print(f"{'timestamp':<32} {'strategy':<22} {'side':<5} {'size':>10} {'price':>8} {'close_pnl':>10}  market")
        for record, close_pnl in rows:
            question = questions.get(record.condition_id, record.condition_id or record.token_id)
            close_str = f"{close_pnl:+.4f}" if close_pnl != 0 else ""
            print(
                f"{record.timestamp.isoformat():<32} {record.strategy:<22} {record.side:<5} "
                f"{record.size:>10.4f} {record.price:>8.4f} {close_str:>10}  {question}"
            )
    finally:
        journal.close()
        store.close()


def cmd_pnl(args: argparse.Namespace) -> None:
    journal = DecisionJournal(args.db_path)
    store = DataStore(args.db_path)
    try:
        portfolio = _replay_portfolio(journal, args.initial_cash, args.window_hours)
        latest_prices = {}
        for token_id in portfolio.positions:
            book = store.get_latest_order_book(token_id)
            if book and book.mid_price is not None:
                latest_prices[token_id] = book.mid_price
        equity = portfolio.mark_to_market(latest_prices)
        unrealized = equity - args.initial_cash - portfolio.realized_pnl

        print(f"Initial cash:   ${args.initial_cash:,.2f}")
        print(f"Current equity: ${equity:,.2f}")
        print(f"Total P&L:      ${equity - args.initial_cash:,.2f}")
        print(f"  Realized:     ${portfolio.realized_pnl:,.2f}")
        print(f"  Unrealized:   ${unrealized:,.2f}")
        print(f"Fills counted:  {len(portfolio.fills)}")
        if settings.dry_run:
            print("\n(dry-run mode: P&L is simulated from logged decisions, not real fills)")
    finally:
        journal.close()
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db-path", default=settings.db_path)
    parser.add_argument("--initial-cash", type=float, default=DEFAULT_INITIAL_CASH)
    parser.add_argument(
        "--window-hours",
        type=float,
        default=DEFAULT_WINDOW_HOURS,
        help=(
            "positions/trades/pnl only replay decisions from the last N hours "
            "(decisions_log has no pruning and can grow to millions of rows - "
            "an unbounded replay was clocked at 9.5M rows/multi-minute in real "
            "operation). A position opened before this window won't be counted."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("positions", help="Show current simulated positions").set_defaults(
        func=cmd_positions
    )

    signals_parser = subparsers.add_parser("signals", help="Show recent signals/quotes")
    signals_parser.add_argument("--limit", type=int, default=20)
    signals_parser.set_defaults(func=cmd_signals)

    subparsers.add_parser("pnl", help="Show current simulated P&L").set_defaults(func=cmd_pnl)

    trades_parser = subparsers.add_parser("trades", help="Show every fill with its own realized P&L")
    trades_parser.add_argument("--limit", type=int, default=0, help="0 = show all")
    trades_parser.set_defaults(func=cmd_trades)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
