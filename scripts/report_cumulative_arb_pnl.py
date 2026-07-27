"""Reports cumulative real P&L for the validated ComplementaryOutcomesSignal
config (taker_fee_bps=200, min_edge_bps=10) across EVERY condition_id ever
collected into the DB by the live-games rotation loop - not just whatever
scripts/refresh_live_games.py currently has in tracked_markets.json, since
main.py restarts drop old (resolved) markets from that file but their
order book history stays in the DB. This is how the running track record
gets checked as scripts/run_live_games_loop.sh accumulates more real hours.

Usage:
    uv run python scripts/report_cumulative_arb_pnl.py
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.store import DataStore  # noqa: E402

from backtest.engine import run_backtest  # noqa: E402
from config import settings  # noqa: E402
from execution.risk import RiskLimits  # noqa: E402
from signals.complementary_outcomes import ComplementaryOutcomesSignal  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--window-hours",
        type=float,
        default=8.0,
        help="Only replay the last N hours - unbounded replay gets slower/heavier every check as the DB grows",
    )
    args = parser.parse_args()
    start = datetime.now(timezone.utc) - timedelta(hours=args.window_hours)

    store = DataStore(settings.db_path)
    try:
        rows = store._conn.execute(
            "SELECT DISTINCT condition_id FROM order_book_snapshots WHERE received_at > ?", (start.isoformat(),)
        ).fetchall()
    finally:
        store.close()
    condition_ids = [r[0] for r in rows]
    print(f"{len(condition_ids)} distinct markets collected in the last {args.window_hours}h")

    risk_limits = RiskLimits.from_settings(settings)
    arb = ComplementaryOutcomesSignal(taker_fee_bps=200, min_edge_bps=10)
    result = run_backtest(
        strategies={"arb": arb},
        condition_ids=condition_ids,
        db_path=settings.db_path,
        start=start,
        risk_limits=risk_limits,
        mode="isolated",
    )["arb"]

    print(
        f"fills={result.num_fills} realized_pnl=${result.realized_pnl:.2f} "
        f"unrealized_pnl=${result.unrealized_pnl:.2f} total_pnl=${result.total_pnl:.2f} "
        f"max_dd={result.max_drawdown:.2%}"
    )
    print(
        "  (realized_pnl is the trustworthy headline number for this strategy: complementary-outcomes "
        "positions are fully hedged complete sets, guaranteed to pay exactly $1 total at resolution "
        "regardless of interim price moves, so only realized_pnl - profit from positions that have "
        "actually settled or round-tripped - is locked in. unrealized_pnl is mark-to-market noise on "
        "still-open positions and can swing in either direction before those positions resolve.)"
    )

    if result.fills:
        fill_timestamps = sorted(f.timestamp for f in result.fills)
        active_span_hours = (fill_timestamps[-1] - fill_timestamps[0]).total_seconds() / 3600
        print(f"active span (first fill to last fill): {active_span_hours:.2f}h")
        if active_span_hours > 0:
            print(
                f"rate over active span (realized): ${result.realized_pnl / active_span_hours:.2f}/hour, "
                f"extrapolated ${result.realized_pnl / active_span_hours * 24:.2f}/day (naive linear - "
                f"assumes this fill rate sustains 24/7, which live game availability does not guarantee)"
            )
    else:
        print("no fills yet")


if __name__ == "__main__":
    main()
