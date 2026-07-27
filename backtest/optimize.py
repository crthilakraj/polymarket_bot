"""Grid-search optimizer built on backtest/engine.py: sweeps each strategy's
*internal* parameters (spread widening, arb edge/fee thresholds, quote size)
and ranks configs by backtest performance. Risk limits (position/order/
portfolio caps) are NOT swept - they represent actual risk tolerance, and
tuning them to chase backtest P&L is just re-labeling "took more risk" as
"optimized," which is a way to overfit, not a way to find a real edge.

IMPORTANT: this ranks configs against ONE historical sample. A config that
wins here can easily be curve-fit to quirks of that specific window rather
than a real, repeatable edge - out-of-sample validation (running the winner
against a held-out later period it never saw) matters far more than the
ranking itself. Treat the winner as a hypothesis to keep testing, not a
conclusion. Configs with very few fills are flagged separately since their
P&L is mostly noise, not signal.

Usage:
    uv run python backtest/optimize.py --strategy complementary_outcomes
    uv run python backtest/optimize.py --strategy market_making
    uv run python backtest/optimize.py --strategy both --min-fills 5
"""

import argparse
import itertools
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.engine import run_backtest  # noqa: E402
from backtest.report import BacktestResult  # noqa: E402
from config import settings  # noqa: E402
from execution.risk import RiskLimits  # noqa: E402
from signals.complementary_outcomes import ComplementaryOutcomesSignal  # noqa: E402
from signals.market_making.models import PositionLimits  # noqa: E402
from signals.market_making.spread import SpreadParams  # noqa: E402
from signals.market_making.strategy import MarketMakingStrategy  # noqa: E402

DEFAULT_MIN_FILLS = 3

TAKER_FEE_BPS_GRID = [0, 50, 100, 200]
MIN_EDGE_BPS_GRID = [10, 25, 50, 100]

BASE_HALF_SPREAD_BPS_GRID = [20, 50, 100, 300, 500, 800]
INVENTORY_WIDEN_MAX_MULTIPLIER_GRID = [2.0, 3.0, 5.0]
QUOTE_SIZE_GRID = [5.0, 10.0, 25.0]


@dataclass(frozen=True)
class SweepResult:
    params: dict
    result: BacktestResult


def sweep_complementary_outcomes(
    condition_ids: list[str], db_path: str, risk_limits: RiskLimits
) -> list[SweepResult]:
    results = []
    for taker_fee_bps, min_edge_bps in itertools.product(TAKER_FEE_BPS_GRID, MIN_EDGE_BPS_GRID):
        strategy = ComplementaryOutcomesSignal(taker_fee_bps=taker_fee_bps, min_edge_bps=min_edge_bps)
        outcome = run_backtest(
            strategies={"complementary_outcomes": strategy},
            condition_ids=condition_ids,
            db_path=db_path,
            risk_limits=risk_limits,
            mode="isolated",
        )["complementary_outcomes"]
        results.append(
            SweepResult(
                params={"taker_fee_bps": taker_fee_bps, "min_edge_bps": min_edge_bps}, result=outcome
            )
        )
    return results


def sweep_market_making(
    condition_ids: list[str], db_path: str, risk_limits: RiskLimits
) -> list[SweepResult]:
    results = []
    for base_half_spread_bps, inventory_widen, quote_size in itertools.product(
        BASE_HALF_SPREAD_BPS_GRID, INVENTORY_WIDEN_MAX_MULTIPLIER_GRID, QUOTE_SIZE_GRID
    ):
        strategy = MarketMakingStrategy(
            position_limits=PositionLimits.from_settings(settings),
            spread_params=SpreadParams(
                base_half_spread_bps=base_half_spread_bps,
                max_half_spread_bps=max(500.0, base_half_spread_bps * inventory_widen),
                inventory_widen_max_multiplier=inventory_widen,
            ),
            quote_size=quote_size,
        )
        outcome = run_backtest(
            strategies={"market_making": strategy},
            condition_ids=condition_ids,
            db_path=db_path,
            risk_limits=risk_limits,
            mode="isolated",
        )["market_making"]
        results.append(
            SweepResult(
                params={
                    "base_half_spread_bps": base_half_spread_bps,
                    "inventory_widen_max_multiplier": inventory_widen,
                    "quote_size": quote_size,
                },
                result=outcome,
            )
        )
    return results


def print_leaderboard(sweep_results: list[SweepResult], min_fills: int) -> SweepResult | None:
    ranked = sorted(sweep_results, key=lambda r: r.result.total_pnl, reverse=True)

    print(f"{'params':<75} {'fills':>6} {'total_pnl':>12} {'sharpe':>8} {'max_dd':>8}")
    for sr in ranked:
        flag = "" if sr.result.num_fills >= min_fills else "  (< min-fills, not trustworthy)"
        print(
            f"{str(sr.params):<75} {sr.result.num_fills:>6} {sr.result.total_pnl:>12.2f} "
            f"{sr.result.sharpe:>8.3f} {sr.result.max_drawdown:>8.2%}{flag}"
        )

    viable = [sr for sr in ranked if sr.result.num_fills >= min_fills]
    if not viable:
        print(
            f"\nNo config reached {min_fills} fills - the historical sample is too thin to "
            "optimize meaningfully here. Collect more data before trusting any ranking above."
        )
        return None

    best = viable[0]
    print(f"\nBest by total P&L with >= {min_fills} fills: {best.params}")
    print(
        f"  total_pnl=${best.result.total_pnl:.2f} sharpe={best.result.sharpe:.3f} "
        f"max_drawdown={best.result.max_drawdown:.2%} fills={best.result.num_fills}"
    )
    print(
        "  Validate this out-of-sample (more/newer data) before trusting it - a single-window "
        "backtest winner is a hypothesis, not a conclusion."
    )
    return best


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--strategy", choices=["complementary_outcomes", "market_making", "both"], default="both")
    parser.add_argument("--condition-ids", default=",".join(settings.market_condition_ids))
    parser.add_argument("--db-path", default=settings.db_path)
    parser.add_argument("--min-fills", type=int, default=DEFAULT_MIN_FILLS)
    args = parser.parse_args()

    condition_ids = [cid.strip() for cid in args.condition_ids.split(",") if cid.strip()]
    if not condition_ids:
        raise SystemExit("no condition_ids given (pass --condition-ids or populate tracked_markets.json)")

    risk_limits = RiskLimits.from_settings(settings)

    if args.strategy in ("complementary_outcomes", "both"):
        print("=== complementary_outcomes ===")
        results = sweep_complementary_outcomes(condition_ids, args.db_path, risk_limits)
        print_leaderboard(results, args.min_fills)
        print()

    if args.strategy in ("market_making", "both"):
        print("=== market_making ===")
        results = sweep_market_making(condition_ids, args.db_path, risk_limits)
        print_leaderboard(results, args.min_fills)


if __name__ == "__main__":
    main()
