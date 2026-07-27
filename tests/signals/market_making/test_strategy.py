from datetime import datetime, timedelta, timezone

import pytest

from data.models import MarketMetadata, OrderBook, PriceLevel
from signals.base import Side
from signals.market_making.models import PositionLimits
from signals.market_making.spread import SpreadParams
from signals.market_making.strategy import MarketMakingStrategy

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_market(end_date=None) -> MarketMetadata:
    return MarketMetadata(
        condition_id="0xcond",
        question_id=None,
        question="Will X happen?",
        description=None,
        resolution_source=None,
        category=None,
        end_date=end_date,
        active=True,
        closed=False,
        outcomes=["Yes", "No"],
        outcome_prices=[0.5, 0.5],
        token_ids=["t1", "t2"],
    )


def make_book(token_id="t1", bid=0.49, ask=0.51) -> OrderBook:
    return OrderBook(
        token_id=token_id,
        condition_id="0xcond",
        bids=[PriceLevel(bid, 1000.0)],
        asks=[PriceLevel(ask, 1000.0)],
        exchange_timestamp=None,
    )


def make_strategy(**overrides) -> MarketMakingStrategy:
    defaults = dict(
        position_limits=PositionLimits(max_position=100.0, max_order_size=25.0),
        spread_params=SpreadParams(),
        quote_size=10.0,
    )
    defaults.update(overrides)
    return MarketMakingStrategy(**defaults)


def test_quote_returns_no_sides_when_book_has_no_liquidity():
    strategy = make_strategy()
    empty_book = OrderBook(
        token_id="t1", condition_id="0xcond", bids=[], asks=[], exchange_timestamp=None
    )

    result = strategy.quote(make_market(), empty_book, now=NOW)

    assert result.bid is None
    assert result.ask is None


def test_quote_centers_on_mid_price_at_neutral_state():
    strategy = make_strategy()
    book = make_book(bid=0.49, ask=0.51)  # mid = 0.50

    result = strategy.quote(make_market(), book, now=NOW)

    mid = 0.50
    assert result.bid.price < mid < result.ask.price
    assert (mid - result.bid.price) == pytest.approx(result.ask.price - mid)


def test_record_fill_updates_tracked_position():
    strategy = make_strategy()

    assert strategy.position("t1") == 0.0
    strategy.record_fill("t1", Side.BUY, 20.0)
    assert strategy.position("t1") == 20.0
    strategy.record_fill("t1", Side.SELL, 5.0)
    assert strategy.position("t1") == 15.0


def test_positions_are_tracked_independently_per_token():
    strategy = make_strategy()

    strategy.record_fill("t1", Side.BUY, 30.0)

    assert strategy.position("t1") == 30.0
    assert strategy.position("t2") == 0.0


def test_inventory_skew_widens_spread():
    strategy = make_strategy()
    market = make_market()
    book = make_book(bid=0.49, ask=0.51)

    baseline = strategy.quote(market, book, now=NOW)
    baseline_spread = baseline.ask.price - baseline.bid.price

    strategy.record_fill("t1", Side.BUY, 50.0)  # half of max_position -> skew 0.5
    skewed = strategy.quote(market, book, now=NOW)
    skewed_spread = skewed.ask.price - skewed.bid.price

    assert skewed_spread > baseline_spread


def test_inventory_close_to_cap_shrinks_the_size_of_the_capped_side():
    strategy = make_strategy(
        position_limits=PositionLimits(max_position=100.0, max_order_size=25.0),
        quote_size=10.0,
    )
    market = make_market()
    book = make_book(bid=0.49, ask=0.51)

    strategy.record_fill("t1", Side.BUY, 95.0)  # only 5 units of room left to buy
    result = strategy.quote(market, book, now=NOW)

    assert result.bid.size == 5.0  # clipped to remaining room, not the full quote_size
    assert result.ask.size == 10.0  # plenty of room to sell back down


def test_hard_position_cap_stops_quoting_the_capped_side():
    strategy = make_strategy(
        position_limits=PositionLimits(max_position=100.0, max_order_size=25.0)
    )
    market = make_market()
    book = make_book()

    strategy.record_fill("t1", Side.BUY, 100.0)  # exactly at the cap

    result = strategy.quote(market, book, now=NOW)

    assert result.bid is None  # no room left to buy
    assert result.ask is not None  # still plenty of room to sell back down


def test_quote_size_never_exceeds_max_order_size():
    strategy = make_strategy(
        position_limits=PositionLimits(max_position=1000.0, max_order_size=5.0),
        quote_size=10.0,
    )

    result = strategy.quote(make_market(), make_book(), now=NOW)

    assert result.bid.size <= 5.0
    assert result.ask.size <= 5.0


def test_quote_widens_as_resolution_approaches():
    strategy = make_strategy(spread_params=SpreadParams(time_widen_horizon_hours=24.0))
    market = make_market(end_date=NOW + timedelta(hours=48))
    book = make_book(bid=0.49, ask=0.51)

    far_from_resolution = strategy.quote(market, book, now=NOW)
    far_spread = far_from_resolution.ask.price - far_from_resolution.bid.price

    near_resolution = strategy.quote(market, book, now=NOW + timedelta(hours=47.5))
    near_spread = near_resolution.ask.price - near_resolution.bid.price

    assert near_spread > far_spread


def test_quote_is_neutral_when_resolution_time_unknown():
    strategy = make_strategy()
    market = make_market(end_date=None)

    result = strategy.quote(market, make_book(bid=0.49, ask=0.51), now=NOW)

    # Base half-spread only (50bps of 0.50 mid = 0.0025 each side).
    assert result.bid.price == pytest.approx(0.50 - 0.50 * 0.005)
    assert result.ask.price == pytest.approx(0.50 + 0.50 * 0.005)
