import pytest

from data.models import MarketMetadata, OrderBook, PriceLevel
from execution import orders as orders_module
from execution.models import OrderStatus
from execution.order_manager import OrderManager
from execution.risk import RiskLimits
from execution.sizing import KellyParams
from signals.base import Side, Signal
from signals.market_making.models import Quote, QuotePair


def make_market(condition_id="0xcond") -> MarketMetadata:
    return MarketMetadata(
        condition_id=condition_id,
        question_id=None,
        question="Will X happen?",
        description=None,
        resolution_source=None,
        category=None,
        end_date=None,
        active=True,
        closed=False,
        outcomes=["Yes", "No"],
        outcome_prices=[0.5, 0.5],
        token_ids=["t1", "t2"],
    )


def make_book(token_id="t1", bid=0.44, ask=0.46) -> OrderBook:
    return OrderBook(
        token_id=token_id,
        condition_id="0xcond",
        bids=[PriceLevel(bid, 1000.0)],
        asks=[PriceLevel(ask, 1000.0)],
        exchange_timestamp=None,
    )


def make_signal(**overrides) -> Signal:
    defaults = dict(edge_estimate=0.20, confidence=1.0, side=Side.BUY, token_id="t1")
    defaults.update(overrides)
    return Signal(**defaults)


def make_manager(**overrides) -> OrderManager:
    defaults = dict(
        risk_limits=RiskLimits(max_position_usd=100.0, max_order_usd=25.0, max_portfolio_exposure_usd=300.0),
        dry_run=True,
        kelly_params=KellyParams(kelly_fraction=1.0),
    )
    defaults.update(overrides)
    return OrderManager(**defaults)


# --- handle_signal: dry run ------------------------------------------------------


def test_handle_signal_dry_run_does_not_touch_a_client():
    manager = make_manager(client=None, dry_run=True)
    decision = manager.handle_signal(make_signal(), make_market(), make_book())

    assert decision.status is OrderStatus.DRY_RUN
    assert decision.intent is not None
    assert decision.intent.side is Side.BUY
    assert decision.intent.token_id == "t1"
    assert decision.intent.price == 0.46  # BUY transacts at the ask


def test_handle_signal_sell_transacts_at_the_bid():
    manager = make_manager()
    signal = make_signal(side=Side.SELL, edge_estimate=0.20)
    decision = manager.handle_signal(signal, make_market(), make_book(bid=0.44, ask=0.46))

    assert decision.intent.price == 0.44


def test_handle_signal_resizes_to_max_order_usd():
    manager = make_manager(
        risk_limits=RiskLimits(max_position_usd=1000.0, max_order_usd=5.0, max_portfolio_exposure_usd=1000.0)
    )
    decision = manager.handle_signal(make_signal(edge_estimate=0.50), make_market(), make_book())

    assert decision.status is OrderStatus.DRY_RUN
    assert decision.intent.price * decision.intent.size == pytest.approx(5.0)
    assert any("max_order_usd" in reason for reason in decision.reasons)


def test_handle_signal_rejects_when_no_token_id():
    manager = make_manager()
    signal = make_signal(token_id=None)
    decision = manager.handle_signal(signal, make_market(), make_book())

    assert decision.status is OrderStatus.REJECTED
    assert decision.intent is None


def test_handle_signal_rejects_when_relevant_side_of_book_is_empty():
    manager = make_manager()
    empty_ask_book = OrderBook(
        token_id="t1", condition_id="0xcond", bids=[PriceLevel(0.44, 10)], asks=[], exchange_timestamp=None
    )
    decision = manager.handle_signal(make_signal(side=Side.BUY), make_market(), empty_ask_book)

    assert decision.status is OrderStatus.REJECTED


def test_handle_signal_rejects_when_kelly_sizing_is_zero():
    manager = make_manager()
    # Negative edge on a BUY signal implies fair value below the ask - no edge to buy.
    signal = make_signal(edge_estimate=-0.10, side=Side.BUY)
    decision = manager.handle_signal(signal, make_market(), make_book())

    assert decision.status is OrderStatus.REJECTED
    assert decision.intent is None


# --- handle_quote (market making) -------------------------------------------------


def test_handle_quote_produces_a_decision_per_side():
    manager = make_manager(
        risk_limits=RiskLimits(max_position_usd=1000.0, max_order_usd=1000.0, max_portfolio_exposure_usd=1000.0)
    )
    quote_pair = QuotePair(token_id="t1", bid=Quote(0.44, 10.0), ask=Quote(0.46, 10.0))

    decisions = manager.handle_quote(quote_pair, make_market())

    assert len(decisions) == 2
    assert {d.intent.side for d in decisions} == {Side.BUY, Side.SELL}
    assert all(d.status is OrderStatus.DRY_RUN for d in decisions)


def test_handle_quote_skips_missing_sides():
    manager = make_manager()
    quote_pair = QuotePair(token_id="t1", bid=Quote(0.44, 10.0), ask=None)

    decisions = manager.handle_quote(quote_pair, make_market())

    assert len(decisions) == 1
    assert decisions[0].intent.side is Side.BUY


# --- handle_multi_leg_signal (complementary_outcomes-style arb) -------------------


def make_multi_leg_signal(**overrides) -> Signal:
    legs = [
        {"token_id": "t1", "side": Side.BUY, "price": 0.45},
        {"token_id": "t2", "side": Side.BUY, "price": 0.48},
    ]
    defaults = dict(
        edge_estimate=0.07,
        confidence=1.0,
        side=Side.BUY,
        token_id=None,
        metadata={"legs": legs, "condition_id": "0xcond"},
    )
    defaults.update(overrides)
    return Signal(**defaults)


def test_handle_multi_leg_signal_submits_one_decision_per_leg_at_equal_share_counts():
    manager = make_manager(
        risk_limits=RiskLimits(max_position_usd=1000.0, max_order_usd=93.0, max_portfolio_exposure_usd=1000.0)
    )
    signal = make_multi_leg_signal()

    decisions = manager.handle_multi_leg_signal(signal, make_market())

    assert len(decisions) == 2
    assert all(d.status is OrderStatus.DRY_RUN for d in decisions)
    sizes = {d.intent.token_id: d.intent.size for d in decisions}
    # basket cost = 0.45 + 0.48 = 0.93; approved $93 -> 100 complete sets, same on both legs.
    assert sizes["t1"] == pytest.approx(100.0)
    assert sizes["t2"] == pytest.approx(100.0)


def test_handle_multi_leg_signal_resizes_to_available_room_keeping_legs_equal():
    manager = make_manager(
        risk_limits=RiskLimits(max_position_usd=1000.0, max_order_usd=9.3, max_portfolio_exposure_usd=1000.0)
    )
    signal = make_multi_leg_signal()

    decisions = manager.handle_multi_leg_signal(signal, make_market())

    sizes = {d.intent.token_id: d.intent.size for d in decisions}
    assert sizes["t1"] == pytest.approx(sizes["t2"])  # still equal shares after resizing
    assert sizes["t1"] == pytest.approx(10.0)  # $9.30 / 0.93 basket cost


def test_handle_multi_leg_signal_rejects_when_no_room():
    manager = make_manager(
        risk_limits=RiskLimits(max_position_usd=20.0, max_order_usd=1000.0, max_portfolio_exposure_usd=1000.0)
    )
    market = make_market()
    # Fully commit the market's exposure cap first.
    manager.handle_signal(make_signal(edge_estimate=0.50), market, make_book())
    assert manager.market_exposure(market.condition_id) == pytest.approx(20.0)

    decisions = manager.handle_multi_leg_signal(make_multi_leg_signal(), market)

    assert len(decisions) == 1
    assert decisions[0].status is OrderStatus.REJECTED


def test_handle_multi_leg_signal_rejects_when_no_legs():
    manager = make_manager()
    signal = make_multi_leg_signal(metadata={})

    decisions = manager.handle_multi_leg_signal(signal, make_market())

    assert len(decisions) == 1
    assert decisions[0].status is OrderStatus.REJECTED


def test_handle_multi_leg_signal_rejects_whole_basket_if_any_leg_is_a_duplicate():
    """Found live: one leg's price repeated from a recent submission (idempotency
    dedup) while the other leg's price had moved, so only the non-duplicate leg
    went through - a real, unhedged single-leg position instead of the intended
    complete-set basket. Submitting the first basket should mark both legs'
    (token, side, price, size) keys as seen; an identical second basket must be
    rejected on BOTH legs together, not partially filled."""
    manager = make_manager(
        risk_limits=RiskLimits(max_position_usd=1000.0, max_order_usd=93.0, max_portfolio_exposure_usd=1000.0)
    )
    market = make_market()

    first = manager.handle_multi_leg_signal(make_multi_leg_signal(), market)
    assert all(d.status is OrderStatus.DRY_RUN for d in first)

    second = manager.handle_multi_leg_signal(make_multi_leg_signal(), market)

    assert len(second) == 2
    assert all(d.status is OrderStatus.REJECTED for d in second)
    assert all("duplicate" in d.reasons[0] for d in second)


def test_multi_leg_and_single_leg_orders_share_the_same_exposure_tracker():
    manager = make_manager(
        risk_limits=RiskLimits(max_position_usd=20.0, max_order_usd=1000.0, max_portfolio_exposure_usd=1000.0)
    )
    market = make_market()

    manager.handle_multi_leg_signal(make_multi_leg_signal(), market)
    assert manager.market_exposure(market.condition_id) == pytest.approx(20.0)  # capped by max_position_usd

    # The market is now fully committed - a single-leg signal on the same
    # market should be rejected by the same per-market cap.
    decision = manager.handle_signal(make_signal(edge_estimate=0.50), market, make_book())
    assert decision.status is OrderStatus.REJECTED


# --- shared risk gate / exposure bookkeeping across both entry points -------------


def test_signal_and_quote_orders_share_the_same_market_exposure_tracking():
    manager = make_manager(
        risk_limits=RiskLimits(max_position_usd=20.0, max_order_usd=1000.0, max_portfolio_exposure_usd=1000.0)
    )
    market = make_market()

    signal_decision = manager.handle_signal(
        make_signal(edge_estimate=0.50, confidence=1.0), market, make_book()
    )
    assert signal_decision.status is OrderStatus.DRY_RUN
    committed_by_signal = signal_decision.intent.price * signal_decision.intent.size
    assert committed_by_signal == pytest.approx(20.0)  # capped by max_position_usd

    # The market is now fully committed - a market-making quote on the same
    # market should be rejected by the exact same per-market cap.
    quote_pair = QuotePair(token_id="t1", bid=Quote(0.44, 10.0), ask=None)
    quote_decisions = manager.handle_quote(quote_pair, market)

    assert quote_decisions[0].status is OrderStatus.REJECTED


def test_total_exposure_accumulates_across_markets():
    manager = make_manager(
        risk_limits=RiskLimits(max_position_usd=1000.0, max_order_usd=1000.0, max_portfolio_exposure_usd=1000.0)
    )
    manager.handle_signal(make_signal(edge_estimate=0.50), make_market("0xcond-a"), make_book())
    manager.handle_signal(make_signal(edge_estimate=0.50), make_market("0xcond-b"), make_book())

    assert manager.total_exposure == pytest.approx(
        manager.market_exposure("0xcond-a") + manager.market_exposure("0xcond-b")
    )
    assert manager.total_exposure > 0


# --- idempotency -------------------------------------------------------------------


def test_duplicate_signal_within_ttl_is_rejected_and_not_double_counted(monkeypatch):
    fake_now = [1000.0]
    monkeypatch.setattr("execution.order_manager.time.monotonic", lambda: fake_now[0])
    manager = make_manager()
    market = make_market()
    book = make_book()
    signal = make_signal()

    first = manager.handle_signal(signal, market, book)
    second = manager.handle_signal(signal, market, book)

    assert first.status is OrderStatus.DRY_RUN
    assert second.status is OrderStatus.REJECTED
    assert "duplicate" in second.reasons[0]
    assert manager.market_exposure(market.condition_id) == pytest.approx(
        first.intent.price * first.intent.size
    )


def test_resubmission_allowed_after_idempotency_ttl_expires():
    fake_now = [1000.0]
    manager = make_manager(idempotency_ttl_seconds=30.0, clock=lambda: fake_now[0])
    market = make_market()
    book = make_book()
    signal = make_signal()

    first = manager.handle_signal(signal, market, book)
    fake_now[0] += 31.0
    second = manager.handle_signal(signal, market, book)

    assert first.status is OrderStatus.DRY_RUN
    assert second.status is OrderStatus.DRY_RUN


# --- live submission path (mocked client) -----------------------------------------


def test_live_submission_calls_place_order_and_records_exposure(monkeypatch):
    calls = []

    def fake_place_order(client, intent):
        calls.append(intent)
        return {"success": True, "orderID": "order-1"}

    monkeypatch.setattr(orders_module, "place_order", fake_place_order)
    manager = make_manager(dry_run=False, client=object())

    decision = manager.handle_signal(make_signal(), make_market(), make_book())

    assert decision.status is OrderStatus.SUBMITTED
    assert decision.order_id == "order-1"
    assert len(calls) == 1
    assert manager.total_exposure > 0


def test_live_submission_reports_failed_status_on_placement_error(monkeypatch):
    def fake_place_order(client, intent):
        raise orders_module.OrderPlacementError("boom")

    monkeypatch.setattr(orders_module, "place_order", fake_place_order)
    manager = make_manager(dry_run=False, client=object())

    decision = manager.handle_signal(make_signal(), make_market(), make_book())

    assert decision.status is OrderStatus.FAILED
    assert manager.total_exposure == 0  # nothing recorded on failure


def test_live_mode_without_client_raises():
    manager = make_manager(dry_run=False, client=None)

    with pytest.raises(RuntimeError):
        manager.handle_signal(make_signal(), make_market(), make_book())
