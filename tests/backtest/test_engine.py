from datetime import datetime, timezone

import pytest

from data.models import MarketMetadata, OrderBook, PriceLevel
from data.store import DataStore
from execution.risk import RiskLimits
from signals.complementary_outcomes import ComplementaryOutcomesSignal
from signals.market_making.models import PositionLimits
from signals.market_making.strategy import MarketMakingStrategy

from backtest.engine import run_backtest


def make_arb_market() -> MarketMetadata:
    return MarketMetadata(
        condition_id="0xarb",
        question_id=None,
        question="Will X happen?",
        description=None,
        resolution_source=None,
        category=None,
        end_date=None,
        active=True,
        closed=True,  # already resolved, for the settlement assertions below
        outcomes=["Yes", "No"],
        outcome_prices=[1.0, 0.0],  # Yes won
        token_ids=["yes", "no"],
    )


def make_mm_market() -> MarketMetadata:
    return MarketMetadata(
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


def seed_arb_data(store: DataStore) -> None:
    store.save_market_metadata(make_arb_market())
    # Underpriced complete set: 0.45 + 0.48 = 0.93 < $1.
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


def seed_mm_data(store: DataStore) -> None:
    store.save_market_metadata(make_mm_market())
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
    # Big move: best_bid now crosses through the resting ask from the first quote.
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


GENEROUS_LIMITS = RiskLimits(max_position_usd=10_000.0, max_order_usd=10_000.0, max_portfolio_exposure_usd=10_000.0)


def test_complementary_outcomes_backtest_fills_and_realizes_pnl_on_resolution(tmp_path):
    db_path = tmp_path / "test.db"
    store = DataStore(db_path)
    seed_arb_data(store)
    store.close()

    result = run_backtest(
        strategies={"arb": ComplementaryOutcomesSignal(taker_fee_bps=0, min_edge_bps=0)},
        condition_ids=["0xarb"],
        db_path=str(db_path),
        risk_limits=GENEROUS_LIMITS,
        initial_cash=1000.0,
        mode="isolated",
    )

    arb_result = result["arb"]
    assert arb_result.num_fills == 2  # one leg per outcome
    assert {fill.token_id for fill in arb_result.fills} == {"yes", "no"}
    # Bought the complete set under $1 and it resolved - should be profitable.
    assert arb_result.realized_pnl > 0
    assert arb_result.total_pnl > 0


def test_market_making_backtest_generates_a_crossing_fill(tmp_path):
    db_path = tmp_path / "test.db"
    store = DataStore(db_path)
    seed_mm_data(store)
    store.close()

    strategy = MarketMakingStrategy(
        position_limits=PositionLimits(max_position=1000.0, max_order_size=1000.0), quote_size=10.0
    )
    result = run_backtest(
        strategies={"mm": strategy},
        condition_ids=["0xmm"],
        db_path=str(db_path),
        risk_limits=GENEROUS_LIMITS,
        initial_cash=1000.0,
        mode="isolated",
    )

    mm_result = result["mm"]
    assert mm_result.num_fills >= 1
    assert any(fill.side.value == "SELL" for fill in mm_result.fills)  # the ask got hit


def test_market_making_fill_survives_a_deduped_static_tick_in_between(tmp_path):
    """Regression test: a resting quote must not be lost from tracking just
    because the book was unchanged for one tick (which OrderManager's
    idempotency guard correctly rejects as a duplicate re-quote) - the
    original order is still resting and must still be eligible to fill on a
    later crossing tick."""
    db_path = tmp_path / "test.db"
    store = DataStore(db_path)
    store.save_market_metadata(make_mm_market())
    # t1: initial quote.
    store.save_order_book(
        OrderBook(
            token_id="mm-token",
            condition_id="0xmm",
            bids=[PriceLevel(0.48, 100)],
            asks=[PriceLevel(0.52, 100)],
            exchange_timestamp=None,
            received_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
        )
    )
    # t2: identical book (well within the idempotency window) - re-quoting
    # produces the exact same intent, which OrderManager dedupes.
    store.save_order_book(
        OrderBook(
            token_id="mm-token",
            condition_id="0xmm",
            bids=[PriceLevel(0.48, 100)],
            asks=[PriceLevel(0.52, 100)],
            exchange_timestamp=None,
            received_at=datetime(2026, 1, 1, 0, 0, 10, tzinfo=timezone.utc),
        )
    )
    # t3: big move crossing the ORIGINAL (t1) resting ask.
    store.save_order_book(
        OrderBook(
            token_id="mm-token",
            condition_id="0xmm",
            bids=[PriceLevel(0.60, 100)],
            asks=[PriceLevel(0.62, 100)],
            exchange_timestamp=None,
            received_at=datetime(2026, 1, 1, 0, 0, 20, tzinfo=timezone.utc),
        )
    )
    store.close()

    strategy = MarketMakingStrategy(
        position_limits=PositionLimits(max_position=1000.0, max_order_size=1000.0), quote_size=10.0
    )
    result = run_backtest(
        strategies={"mm": strategy},
        condition_ids=["0xmm"],
        db_path=str(db_path),
        risk_limits=GENEROUS_LIMITS,
        initial_cash=1000.0,
        mode="isolated",
    )

    mm_result = result["mm"]
    assert mm_result.num_fills >= 1
    sell_fills = [fill for fill in mm_result.fills if fill.side.value == "SELL"]
    assert sell_fills
    # Filled at the ORIGINAL (t1) resting ask MarketMakingStrategy quoted
    # (mid 0.50 + default 50bps half-spread = 0.5025), not a later re-quote.
    assert sell_fills[0].price == pytest.approx(0.5025)


def test_isolated_mode_returns_a_result_per_strategy(tmp_path):
    db_path = tmp_path / "test.db"
    store = DataStore(db_path)
    seed_arb_data(store)
    seed_mm_data(store)
    store.close()

    strategies = {
        "arb": ComplementaryOutcomesSignal(taker_fee_bps=0, min_edge_bps=0),
        "mm": MarketMakingStrategy(position_limits=PositionLimits(max_position=1000.0, max_order_size=1000.0)),
    }
    result = run_backtest(
        strategies=strategies,
        condition_ids=["0xarb", "0xmm"],
        db_path=str(db_path),
        risk_limits=GENEROUS_LIMITS,
        mode="isolated",
    )

    assert set(result.keys()) == {"arb", "mm"}
    assert result["arb"].num_fills > 0
    # Each isolated run starts from the same initial_cash independently.
    assert result["arb"].initial_cash == result["mm"].initial_cash


def test_combined_mode_returns_a_single_result_with_per_strategy_attribution(tmp_path):
    db_path = tmp_path / "test.db"
    store = DataStore(db_path)
    seed_arb_data(store)
    seed_mm_data(store)
    store.close()

    strategies = {
        "arb": ComplementaryOutcomesSignal(taker_fee_bps=0, min_edge_bps=0),
        "mm": MarketMakingStrategy(position_limits=PositionLimits(max_position=1000.0, max_order_size=1000.0)),
    }
    result = run_backtest(
        strategies=strategies,
        condition_ids=["0xarb", "0xmm"],
        db_path=str(db_path),
        risk_limits=GENEROUS_LIMITS,
        mode="combined",
    )

    assert result.strategy_names == ["arb", "mm"]
    fill_strategies = {fill.strategy for fill in result.fills}
    assert "arb" in fill_strategies
    assert "mm" in fill_strategies


def test_date_range_excludes_data_outside_the_window(tmp_path):
    db_path = tmp_path / "test.db"
    store = DataStore(db_path)
    seed_arb_data(store)
    store.close()

    result = run_backtest(
        strategies={"arb": ComplementaryOutcomesSignal(taker_fee_bps=0, min_edge_bps=0)},
        condition_ids=["0xarb"],
        db_path=str(db_path),
        start=datetime(2026, 2, 1, tzinfo=timezone.utc),  # after all seeded data
        risk_limits=GENEROUS_LIMITS,
        mode="isolated",
    )

    assert result["arb"].num_fills == 0


def test_run_backtest_rejects_unknown_mode(tmp_path):
    db_path = tmp_path / "test.db"
    DataStore(db_path).close()

    try:
        run_backtest(
            strategies={"arb": ComplementaryOutcomesSignal()},
            condition_ids=["0xarb"],
            db_path=str(db_path),
            mode="bogus",
        )
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for an invalid mode")
