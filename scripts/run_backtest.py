"""Standalone backtest runner: replays stored historical order book data
(collected via scripts/run_data_layer.py) through one or more strategies and
execution/, with no live API calls, and writes a text report + equity curve
plot per result.

Usage:
    uv run python scripts/run_backtest.py --strategies complementary_outcomes,market_making
    uv run python scripts/run_backtest.py --strategies market_making --start 2026-01-01 --end 2026-02-01 --mode combined

Requires DB_PATH to already contain order book snapshots + market metadata
for the given --condition-ids.
"""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.engine import run_backtest
from backtest.report import generate_report, plot_equity_curve
from config import settings
from execution.risk import RiskLimits
from logging_config import configure_logging
from signals.complementary_outcomes import ComplementaryOutcomesSignal
from signals.market_making.models import PositionLimits
from signals.market_making.strategy import MarketMakingStrategy
from signals.news.claude_assessor import ClaudeNewsAssessor
from signals.news.embeddings import FastEmbedEmbedder
from signals.news.signal import NewsEdgeSignal

logger = logging.getLogger(__name__)

STRATEGY_BUILDERS = {
    "complementary_outcomes": lambda: ComplementaryOutcomesSignal(),
    "market_making": lambda: MarketMakingStrategy(position_limits=PositionLimits.from_settings(settings)),
    "news": lambda: NewsEdgeSignal(embedder=FastEmbedEmbedder(), assessor=ClaudeNewsAssessor()),
}


def _parse_date(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strategies",
        default="complementary_outcomes",
        help=f"Comma-separated subset of: {', '.join(STRATEGY_BUILDERS)}",
    )
    parser.add_argument(
        "--condition-ids",
        default=",".join(settings.market_condition_ids),
        help="Comma-separated condition_ids (defaults to MARKET_CONDITION_IDS)",
    )
    parser.add_argument("--start", default=None, help="ISO date/time, e.g. 2026-01-01")
    parser.add_argument("--end", default=None, help="ISO date/time")
    parser.add_argument("--mode", choices=["isolated", "combined"], default="isolated")
    parser.add_argument("--db-path", default=settings.db_path)
    parser.add_argument("--initial-cash", type=float, default=1000.0)
    parser.add_argument("--output-dir", default="backtest_output")
    args = parser.parse_args()

    strategy_names = [name.strip() for name in args.strategies.split(",") if name.strip()]
    unknown = set(strategy_names) - set(STRATEGY_BUILDERS)
    if unknown:
        raise SystemExit(f"Unknown strategies: {', '.join(unknown)}. Choose from: {', '.join(STRATEGY_BUILDERS)}")
    if "news" in strategy_names:
        logger.warning(
            "the 'news' strategy has no historical headline feed wired into this generic "
            "backtest engine, so it will never fire here - included for completeness only"
        )

    condition_ids = [cid.strip() for cid in args.condition_ids.split(",") if cid.strip()]
    if not condition_ids:
        raise SystemExit("no condition_ids given (pass --condition-ids or set MARKET_CONDITION_IDS)")

    strategies = {name: STRATEGY_BUILDERS[name]() for name in strategy_names}
    risk_limits = RiskLimits.from_settings(settings)

    logger.info(
        "running backtest: strategies=%s condition_ids=%s mode=%s start=%s end=%s",
        strategy_names,
        condition_ids,
        args.mode,
        args.start,
        args.end,
    )
    results = run_backtest(
        strategies=strategies,
        condition_ids=condition_ids,
        db_path=args.db_path,
        start=_parse_date(args.start),
        end=_parse_date(args.end),
        risk_limits=risk_limits,
        initial_cash=args.initial_cash,
        mode=args.mode,
    )

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    results_by_name = results if isinstance(results, dict) else {"+".join(strategy_names): results}
    for name, result in results_by_name.items():
        report_text = generate_report(result)
        print(f"\n{report_text}\n")
        (output_dir / f"{name}_report.txt").write_text(report_text)
        try:
            plot_equity_curve(result, str(output_dir / f"{name}_equity_curve.png"))
        except ValueError:
            logger.warning("no equity curve data for %s, skipping plot", name)


if __name__ == "__main__":
    main()
