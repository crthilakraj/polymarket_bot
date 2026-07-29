import pytest

from data.models import MarketMetadata, OrderBook, PriceLevel
from signals.base import Side, SignalContext
from signals.complementary_outcomes import ComplementaryOutcomesSignal


def make_market(
    token_ids: list[str], fee_rate: float | None = None, fee_exponent: float | None = None
) -> MarketMetadata:
    return MarketMetadata(
        condition_id="0xcond",
        question_id=None,
        question="Which outcome wins?",
        description=None,
        resolution_source=None,
        category=None,
        end_date=None,
        active=True,
        closed=False,
        outcomes=[f"Outcome {i}" for i in range(len(token_ids))],
        outcome_prices=[],
        token_ids=token_ids,
        fee_rate=fee_rate,
        fee_exponent=fee_exponent,
    )


def make_book(token_id: str, bid: float, ask: float) -> OrderBook:
    return OrderBook(
        token_id=token_id,
        condition_id="0xcond",
        bids=[PriceLevel(bid, 100.0)],
        asks=[PriceLevel(ask, 100.0)],
        exchange_timestamp=None,
    )


def test_flags_underpriced_complete_set_as_buy_arb():
    # Binary market: buying both legs costs 0.95 but pays out $1 - a real arb
    # even after a 2% per-leg fee (fee cost = 0.95 * 0.02 = 0.019).
    market = make_market(["yes", "no"])
    order_books = {
        "yes": make_book("yes", bid=0.44, ask=0.45),
        "no": make_book("no", bid=0.49, ask=0.50),
    }
    signal = ComplementaryOutcomesSignal(taker_fee_bps=200, min_edge_bps=50)

    result = signal.evaluate(market, order_books["yes"], SignalContext(order_books=order_books))

    assert result is not None
    assert result.side is Side.BUY
    expected_fee = (0.45 + 0.50) * 0.02
    expected_edge = 1.0 - 0.95 - expected_fee
    assert result.edge_estimate == pytest.approx(expected_edge)
    assert result.metadata["sum_probability"] == pytest.approx(0.95)
    assert {leg["token_id"] for leg in result.metadata["legs"]} == {"yes", "no"}


def test_flags_overpriced_complete_set_as_sell_arb():
    # Both bids sum to 1.10 - selling (post-mint) a complete set nets a profit.
    market = make_market(["yes", "no"])
    order_books = {
        "yes": make_book("yes", bid=0.58, ask=0.60),
        "no": make_book("no", bid=0.52, ask=0.54),
    }
    signal = ComplementaryOutcomesSignal(taker_fee_bps=200, min_edge_bps=50)

    result = signal.evaluate(market, order_books["yes"], SignalContext(order_books=order_books))

    assert result is not None
    assert result.side is Side.SELL
    expected_fee = (0.58 + 0.52) * 0.02
    expected_edge = (0.58 + 0.52) - 1.0 - expected_fee
    assert result.edge_estimate == pytest.approx(expected_edge)
    assert result.metadata["sum_probability"] == pytest.approx(1.10)


def test_efficient_market_produces_no_signal():
    # Sum of asks (1.01) and bids (0.99) both sit right around $1 - no
    # exploitable edge once fees are considered.
    market = make_market(["yes", "no"])
    order_books = {
        "yes": make_book("yes", bid=0.49, ask=0.50),
        "no": make_book("no", bid=0.50, ask=0.51),
    }
    signal = ComplementaryOutcomesSignal(taker_fee_bps=200, min_edge_bps=50)

    result = signal.evaluate(market, order_books["yes"], SignalContext(order_books=order_books))

    assert result is None


def test_deviation_that_only_covers_fees_does_not_fire():
    # sum_ask = 0.98: a 2% raw deviation, but fee cost (~2%) eats almost all
    # of it, leaving less than the required 0.5% min edge - should not fire.
    market = make_market(["yes", "no"])
    order_books = {
        "yes": make_book("yes", bid=0.48, ask=0.49),
        "no": make_book("no", bid=0.48, ask=0.49),
    }
    signal = ComplementaryOutcomesSignal(taker_fee_bps=200, min_edge_bps=50)

    result = signal.evaluate(market, order_books["yes"], SignalContext(order_books=order_books))

    assert result is None


def test_uses_real_per_market_fee_schedule_when_present():
    # Polymarket's real fee = rate * price**exponent * (1-price)**exponent per
    # share (see help.polymarket.com/en/articles/13364478) - asymmetric,
    # unlike the flat placeholder: cheap/favorite-vs-longshot legs get very
    # different fees. A market with fee_rate set must use this formula
    # instead of the flat taker_fee_bps fallback, even if one is configured.
    market = make_market(["favorite", "longshot"], fee_rate=0.05, fee_exponent=1.0)
    order_books = {
        "favorite": make_book("favorite", bid=0.88, ask=0.90),
        "longshot": make_book("longshot", bid=0.08, ask=0.09),
    }
    signal = ComplementaryOutcomesSignal(taker_fee_bps=200, min_edge_bps=0)

    result = signal.evaluate(
        market, order_books["favorite"], SignalContext(order_books=order_books)
    )

    assert result is not None
    expected_fee = 0.05 * 0.90 * (1 - 0.90) + 0.05 * 0.09 * (1 - 0.09)
    expected_edge = 1.0 - (0.90 + 0.09) - expected_fee
    assert result.metadata["fee_cost"] == pytest.approx(expected_fee)
    assert result.edge_estimate == pytest.approx(expected_edge)
    # Sanity check this really differs from the flat 2% placeholder - proves
    # the real formula path, not a coincidental match.
    flat_fee = (0.90 + 0.09) * 0.02
    assert expected_fee != pytest.approx(flat_fee)


def test_fee_rate_for_matches_real_fee_schedule():
    market = make_market(["yes", "no"], fee_rate=0.05, fee_exponent=1.0)
    signal = ComplementaryOutcomesSignal()

    rate = signal.fee_rate_for(0.90, market)

    assert rate == pytest.approx(0.05 * (1 - 0.90))


def test_fee_rate_for_falls_back_to_flat_rate_without_a_fee_schedule():
    market = make_market(["yes", "no"])  # fee_rate=None
    signal = ComplementaryOutcomesSignal(taker_fee_bps=200)

    rate = signal.fee_rate_for(0.90, market)

    assert rate == pytest.approx(0.02)


def test_missing_book_for_an_outcome_returns_none():
    market = make_market(["yes", "no"])
    order_books = {"yes": make_book("yes", bid=0.40, ask=0.41)}
    signal = ComplementaryOutcomesSignal()

    result = signal.evaluate(market, order_books["yes"], SignalContext(order_books=order_books))

    assert result is None


def test_one_sided_book_returns_none():
    market = make_market(["yes", "no"])
    empty_ask_book = OrderBook(
        token_id="no",
        condition_id="0xcond",
        bids=[PriceLevel(0.5, 10)],
        asks=[],
        exchange_timestamp=None,
    )
    order_books = {"yes": make_book("yes", bid=0.44, ask=0.45), "no": empty_ask_book}
    signal = ComplementaryOutcomesSignal()

    result = signal.evaluate(market, order_books["yes"], SignalContext(order_books=order_books))

    assert result is None


def test_missing_context_returns_none():
    market = make_market(["yes", "no"])
    signal = ComplementaryOutcomesSignal()

    result = signal.evaluate(market, make_book("yes", bid=0.44, ask=0.45), context=None)

    assert result is None


def test_single_outcome_market_returns_none():
    market = make_market(["only"])
    order_books = {"only": make_book("only", bid=0.99, ask=1.0)}
    signal = ComplementaryOutcomesSignal()

    result = signal.evaluate(market, order_books["only"], SignalContext(order_books=order_books))

    assert result is None


def test_multi_outcome_market_sums_more_than_two_legs():
    # Three-way market: asks sum to 0.90 - a clear buy arb.
    market = make_market(["a", "b", "c"])
    order_books = {
        "a": make_book("a", bid=0.29, ask=0.30),
        "b": make_book("b", bid=0.29, ask=0.30),
        "c": make_book("c", bid=0.29, ask=0.30),
    }
    signal = ComplementaryOutcomesSignal(taker_fee_bps=200, min_edge_bps=50)

    result = signal.evaluate(market, order_books["a"], SignalContext(order_books=order_books))

    assert result is not None
    assert result.side is Side.BUY
    assert len(result.metadata["legs"]) == 3


def test_confidence_increases_with_edge_magnitude():
    market = make_market(["yes", "no"])
    signal = ComplementaryOutcomesSignal(taker_fee_bps=200, min_edge_bps=50)

    small_edge_books = {
        "yes": make_book("yes", bid=0.46, ask=0.47),
        "no": make_book("no", bid=0.49, ask=0.50),
    }
    large_edge_books = {
        "yes": make_book("yes", bid=0.24, ask=0.25),
        "no": make_book("no", bid=0.29, ask=0.30),
    }

    small_result = signal.evaluate(
        market, small_edge_books["yes"], SignalContext(order_books=small_edge_books)
    )
    large_result = signal.evaluate(
        market, large_edge_books["yes"], SignalContext(order_books=large_edge_books)
    )

    assert small_result is not None and large_result is not None
    assert 0.0 <= small_result.confidence <= 1.0
    assert 0.0 <= large_result.confidence <= 1.0
    assert large_result.confidence > small_result.confidence
    assert large_result.confidence == 1.0  # far past 4x the min edge - clipped
