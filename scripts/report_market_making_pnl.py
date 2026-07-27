"""Reports cumulative P&L for MarketMakingStrategy, replayed isolated (its own
fresh portfolio, not competing with ComplementaryOutcomesSignal for exposure
caps) over the live sports/esports game market data collected by
scripts/run_live_games_loop.sh.

MarketMakingStrategy has been running live in dry-run this whole session
(main.py's build_strategies() always includes it - see main.py), but its
performance on sports markets was never actually measured: it was only ever
ruled out on political/Fed markets (see HANDOVER.md), and
report_cumulative_arb_pnl.py / checkpoint_and_prune.py both hard-code
ComplementaryOutcomesSignal only. This script closes that gap using the same
_process_market_making replay path backtest/engine.py already has.

Usage:
    uv run python scripts/report_market_making_pnl.py --window-hours 4
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
from signals.market_making.models import PositionLimits  # noqa: E402
from signals.market_making.strategy import MarketMakingStrategy  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--window-hours", type=float, default=4.0)
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
    mm = MarketMakingStrategy(position_limits=PositionLimits.from_settings(settings))
    result = run_backtest(
        strategies={"mm": mm},
        condition_ids=condition_ids,
        db_path=settings.db_path,
        start=start,
        risk_limits=risk_limits,
        mode="isolated",
    )["mm"]

    print(
        f"fills={result.num_fills} realized_pnl=${result.realized_pnl:.2f} "
        f"unrealized_pnl=${result.unrealized_pnl:.2f} total_pnl=${result.total_pnl:.2f} "
        f"max_dd={result.max_drawdown:.2%}"
    )
    print(
        "  (unlike complementary-outcomes, market-making inventory is NOT hedged - "
        "unrealized_pnl here is real directional exposure risk, not just noise on a "
        "guaranteed-payout position. Weigh both numbers.)"
    )

    if result.fills:
        fill_timestamps = sorted(f.timestamp for f in result.fills)
        active_span_hours = (fill_timestamps[-1] - fill_timestamps[0]).total_seconds() / 3600
        print(f"active span (first fill to last fill): {active_span_hours:.2f}h")
    else:
        print("no fills yet")


if __name__ == "__main__":
    main()
