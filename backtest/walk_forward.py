"""Walk-forward consistency check: splits the historical window into
consecutive slices and runs each slice through run_backtest() independently
(fresh portfolio per slice), so a strategy's edge can be judged by how often
it's profitable across slices, not just its total P&L over the whole window.

A config that only looks good because of one lucky slice is exactly the
overfitting failure mode backtest/optimize.py's docstring warns about - this
is the tool that actually checks for it. "Profitable in every slice" with
enough fills per slice to trust the number is the bar for calling something
a real, repeatable edge rather than noise.

Usage:
    uv run python backtest/walk_forward.py --strategy market_making \\
        --slice-minutes 5 --min-fills-per-slice 1
"""

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.store import DataStore  # noqa: E402

from backtest.engine import run_backtest  # noqa: E402
from backtest.report import BacktestResult  # noqa: E402
from config import settings  # noqa: E402
from execution.risk import RiskLimits  # noqa: E402
from signals.complementary_outcomes import ComplementaryOutcomesSignal  # noqa: E402
from signals.market_making.models import PositionLimits  # noqa: E402
from signals.market_making.strategy import MarketMakingStrategy  # noqa: E402


@dataclass(frozen=True)
class SliceResult:
    start: datetime
    end: datetime
    result: BacktestResult


def data_time_bounds(db_path: str, condition_ids: list[str]) -> tuple[datetime, datetime] | None:
    store = DataStore(db_path)
    try:
        placeholders = ",".join("?" for _ in condition_ids)
        row = store._conn.execute(
            f"SELECT MIN(received_at), MAX(received_at) FROM order_book_snapshots "
            f"WHERE condition_id IN ({placeholders})",
            condition_ids,
        ).fetchone()
    finally:
        store.close()
    if row is None or row[0] is None:
        return None
    return datetime.fromisoformat(row[0]), datetime.fromisoformat(row[1])


def walk_forward(
    strategy_factory,
    condition_ids: list[str],
    db_path: str,
    risk_limits: RiskLimits,
    slice_minutes: float,
    initial_cash: float,
) -> list[SliceResult]:
    bounds = data_time_bounds(db_path, condition_ids)
    if bounds is None:
        return []
    start, end = bounds
    slice_len = timedelta(minutes=slice_minutes)

    slices = []
    cursor = start
    while cursor < end:
        slice_end = min(cursor + slice_len, end)
        result = run_backtest(
            strategies={"s": strategy_factory()},
            condition_ids=condition_ids,
            db_path=db_path,
            start=cursor,
            end=slice_end,
            risk_limits=risk_limits,
            initial_cash=initial_cash,
            mode="isolated",
        )["s"]
        slices.append(SliceResult(start=cursor, end=slice_end, result=result))
        cursor = slice_end

    return slices


def print_slices(slices: list[SliceResult], min_fills_per_slice: int) -> None:
    if not slices:
        print("No data in range.")
        return

    print(f"{'slice_start':<26} {'fills':>6} {'total_pnl':>12} {'return_pct':>10}")
    profitable = 0
    trustworthy = 0
    for s in slices:
        r = s.result
        flag = "" if r.num_fills >= min_fills_per_slice else "  (< min fills, noise)"
        print(f"{s.start.isoformat():<26} {r.num_fills:>6} {r.total_pnl:>12.2f} {r.total_return_pct:>10.2%}{flag}")
        if r.num_fills >= min_fills_per_slice:
            trustworthy += 1
            if r.total_pnl > 0:
                profitable += 1

    span_hours = (slices[-1].end - slices[0].start).total_seconds() / 3600
    total_pnl = sum(s.result.total_pnl for s in slices)
    total_fills = sum(s.result.num_fills for s in slices)
    initial_cash = slices[0].result.initial_cash
    print(f"\nTotal fills: {total_fills}, total P&L: ${total_pnl:.2f} over {span_hours:.2f}h")
    if span_hours > 0 and initial_cash:
        daily_return_pct = (total_pnl / initial_cash) * (24 / span_hours)
        print(f"Extrapolated daily return (naive, linear extrapolation): {daily_return_pct:+.2%}/day")
    if trustworthy:
        print(f"Profitable slices: {profitable}/{trustworthy} trustworthy slices (>= {min_fills_per_slice} fills)")
    else:
        print("No slice reached min-fills-per-slice - too little data to judge consistency at all.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--strategy", choices=["complementary_outcomes", "market_making"], required=True)
    parser.add_argument("--condition-ids", default=",".join(settings.market_condition_ids))
    parser.add_argument("--db-path", default=settings.db_path)
    parser.add_argument("--slice-minutes", type=float, default=5.0)
    parser.add_argument("--min-fills-per-slice", type=int, default=1)
    parser.add_argument("--taker-fee-bps", type=float, default=0.0)
    parser.add_argument("--min-edge-bps", type=float, default=10.0)
    args = parser.parse_args()

    condition_ids = [cid.strip() for cid in args.condition_ids.split(",") if cid.strip()]
    if not condition_ids:
        raise SystemExit("no condition_ids given")

    risk_limits = RiskLimits.from_settings(settings)

    if args.strategy == "complementary_outcomes":
        factory = lambda: ComplementaryOutcomesSignal(  # noqa: E731
            taker_fee_bps=args.taker_fee_bps, min_edge_bps=args.min_edge_bps
        )
    else:
        factory = lambda: MarketMakingStrategy(  # noqa: E731
            position_limits=PositionLimits.from_settings(settings)
        )

    slices = walk_forward(
        factory, condition_ids, args.db_path, risk_limits, args.slice_minutes, settings.max_portfolio_exposure_usd
    )
    print_slices(slices, args.min_fills_per_slice)


if __name__ == "__main__":
    main()
