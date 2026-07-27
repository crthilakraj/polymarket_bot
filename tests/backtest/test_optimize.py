from datetime import datetime, timezone

from data.models import MarketMetadata, OrderBook, PriceLevel
from data.store import DataStore
from execution.risk import RiskLimits

from backtest.optimize import (
    SweepResult,
    print_leaderboard,
    sweep_complementary_outcomes,
    sweep_market_making,
)
from backtest.report import BacktestResult

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

GENEROUS_LIMITS = RiskLimits(max_position_usd=10_000.0, max_order_usd=10_000.0, max_portfolio_exposure_usd=10_000.0)


def make_result(total_pnl: float, num_fills: int) -> BacktestResult:
    return BacktestResult(
        strategy_names=["s"],
        initial_cash=1000.0,
        final_equity=1000.0 + total_pnl,
        realized_pnl=total_pnl,
        total_pnl=total_pnl,
        num_fills=num_fills,
        fills=[],
        equity_curve=[(NOW, 1000.0), (NOW, 1000.0 + total_pnl)],
    )


def test_print_leaderboard_picks_highest_pnl_meeting_min_fills(capsys):
    results = [
        SweepResult(params={"a": 1}, result=make_result(total_pnl=50.0, num_fills=1)),  # lucky, ignored
        SweepResult(params={"a": 2}, result=make_result(total_pnl=20.0, num_fills=10)),
        SweepResult(params={"a": 3}, result=make_result(total_pnl=5.0, num_fills=10)),
    ]

    best = print_leaderboard(results, min_fills=3)

    assert best is not None
    assert best.params == {"a": 2}


def test_print_leaderboard_returns_none_when_nothing_meets_min_fills(capsys):
    results = [SweepResult(params={"a": 1}, result=make_result(total_pnl=50.0, num_fills=1))]

    best = print_leaderboard(results, min_fills=3)

    assert best is None
    assert "too thin" in capsys.readouterr().out


def test_print_leaderboard_ignores_high_pnl_configs_below_min_fills(capsys):
    results = [
        SweepResult(params={"a": 1}, result=make_result(total_pnl=1000.0, num_fills=1)),
        SweepResult(params={"a": 2}, result=make_result(total_pnl=1.0, num_fills=5)),
    ]

    best = print_leaderboard(results, min_fills=3)

    assert best.params == {"a": 2}


def seed_arb_data(store: DataStore) -> None:
    market = MarketMetadata(
        condition_id="0xarb",
        question_id=None,
        question="Will X happen?",
        description=None,
        resolution_source=None,
        category=None,
        end_date=None,
        active=True,
        closed=True,
        outcomes=["Yes", "No"],
        outcome_prices=[1.0, 0.0],
        token_ids=["yes", "no"],
    )
    store.save_market_metadata(market)
    store.save_order_book(
        OrderBook(
            token_id="yes",
            condition_id="0xarb",
            bids=[PriceLevel(0.43, 100)],
            asks=[PriceLevel(0.45, 100)],
            exchange_timestamp=None,
            received_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )
    store.save_order_book(
        OrderBook(
            token_id="no",
            condition_id="0xarb",
            bids=[PriceLevel(0.46, 100)],
            asks=[PriceLevel(0.48, 100)],
            exchange_timestamp=None,
            received_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
        )
    )


def test_sweep_complementary_outcomes_covers_the_full_grid(tmp_path):
    db_path = tmp_path / "test.db"
    store = DataStore(db_path)
    seed_arb_data(store)
    store.close()

    results = sweep_complementary_outcomes(["0xarb"], str(db_path), GENEROUS_LIMITS)

    assert len(results) == 4 * 4  # TAKER_FEE_BPS_GRID x MIN_EDGE_BPS_GRID
    # A zero-fee, zero-min-edge config should catch the arb (0.45+0.48=0.93 < $1).
    zero_fee = next(r for r in results if r.params == {"taker_fee_bps": 0, "min_edge_bps": 10})
    assert zero_fee.result.num_fills > 0


def seed_mm_data(store: DataStore) -> None:
    market = MarketMetadata(
        condition_id="0xmm",
        question_id=None,
        question="Will Y happen?",
        description=None,
        resolution_source=None,
        category=None,
        end_date=None,
        active=True,
        closed=False,
        outcomes=["Yes", "No"],
        outcome_prices=[0.5, 0.5],
        token_ids=["mm-token"],
    )
    store.save_market_metadata(market)
    store.save_order_book(
        OrderBook(
            token_id="mm-token",
            condition_id="0xmm",
            bids=[PriceLevel(0.48, 100)],
            asks=[PriceLevel(0.52, 100)],
            exchange_timestamp=None,
            received_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )
    store.save_order_book(
        OrderBook(
            token_id="mm-token",
            condition_id="0xmm",
            bids=[PriceLevel(0.60, 100)],
            asks=[PriceLevel(0.62, 100)],
            exchange_timestamp=None,
            received_at=datetime(2026, 1, 1, 0, 1, 0, tzinfo=timezone.utc),
        )
    )


def test_sweep_market_making_covers_the_full_grid(tmp_path):
    db_path = tmp_path / "test.db"
    store = DataStore(db_path)
    seed_mm_data(store)
    store.close()

    results = sweep_market_making(["0xmm"], str(db_path), GENEROUS_LIMITS)

    assert len(results) == 6 * 3 * 3
    assert any(r.result.num_fills > 0 for r in results)
