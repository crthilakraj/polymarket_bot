import dataclasses

import pytest

import main as main_module
from config import settings as real_settings
from data.models import MarketMetadata, OrderBook, PriceLevel
from execution.journal import DecisionJournal
from execution.order_manager import OrderManager
from execution.risk import RiskLimits
from signals.complementary_outcomes import ComplementaryOutcomesSignal
from signals.market_making.models import PositionLimits
from signals.market_making.strategy import MarketMakingStrategy


def make_market(**overrides) -> MarketMetadata:
    defaults = dict(
        condition_id="0xcond",
        question_id=None,
        question="Q?",
        description=None,
        resolution_source=None,
        category=None,
        end_date=None,
        active=True,
        closed=False,
        outcomes=["Yes", "No"],
        outcome_prices=[0.5, 0.5],
        token_ids=["yes", "no"],
    )
    defaults.update(overrides)
    return MarketMetadata(**defaults)


def make_book(token_id="yes", bid=0.44, ask=0.46) -> OrderBook:
    return OrderBook(
        token_id=token_id,
        condition_id="0xcond",
        bids=[PriceLevel(bid, 100)],
        asks=[PriceLevel(ask, 100)],
        exchange_timestamp=None,
    )


def test_build_strategies_returns_the_expected_defaults():
    strategies = main_module.build_strategies()

    assert set(strategies) == {"complementary_outcomes"}
    assert isinstance(strategies["complementary_outcomes"], ComplementaryOutcomesSignal)


def test_build_strategies_includes_market_making_when_enabled(monkeypatch):
    monkeypatch.setattr(
        main_module, "settings", dataclasses.replace(real_settings, enable_market_making=True)
    )

    strategies = main_module.build_strategies()

    assert set(strategies) == {"complementary_outcomes", "market_making"}
    assert isinstance(strategies["market_making"], MarketMakingStrategy)


def test_build_order_manager_succeeds_in_dry_run(monkeypatch):
    monkeypatch.setattr(main_module, "settings", dataclasses.replace(real_settings, dry_run=True))

    order_manager = main_module.build_order_manager()

    assert isinstance(order_manager, OrderManager)


def test_build_order_manager_raises_without_explicit_live_confirmation(monkeypatch):
    monkeypatch.setattr(
        main_module,
        "settings",
        dataclasses.replace(real_settings, dry_run=False, live_trading_confirmed=False),
    )

    with pytest.raises(RuntimeError, match="LIVE_TRADING_CONFIRMED"):
        main_module.build_order_manager()


def test_build_order_manager_refuses_to_start_live_when_geoblocked(monkeypatch):
    import execution.client as client_module

    monkeypatch.setattr(
        main_module,
        "settings",
        dataclasses.replace(real_settings, dry_run=False, live_trading_confirmed=True),
    )
    monkeypatch.setattr(
        client_module,
        "check_geoblock",
        lambda: {"blocked": True, "ip": "1.2.3.4", "country": "GB", "region": "ENG"},
    )

    with pytest.raises(SystemExit, match="blocking trading"):
        main_module.build_order_manager()


def test_build_order_manager_refuses_to_start_live_when_account_closed_only(monkeypatch):
    import execution.client as client_module

    class _FakeClient:
        def get_closed_only_mode(self):
            return {"closed_only": True}

    monkeypatch.setattr(
        main_module,
        "settings",
        dataclasses.replace(real_settings, dry_run=False, live_trading_confirmed=True),
    )
    monkeypatch.setattr(client_module, "check_geoblock", lambda: {"blocked": False})
    monkeypatch.setattr(client_module, "get_client", lambda: _FakeClient())

    with pytest.raises(SystemExit, match="closed-only"):
        main_module.build_order_manager()


def test_build_order_manager_proceeds_live_when_both_checks_pass(monkeypatch):
    import execution.client as client_module

    class _FakeClient:
        def get_closed_only_mode(self):
            return {"closed_only": False}

    monkeypatch.setattr(
        main_module,
        "settings",
        dataclasses.replace(real_settings, dry_run=False, live_trading_confirmed=True),
    )
    monkeypatch.setattr(client_module, "check_geoblock", lambda: {"blocked": False})
    monkeypatch.setattr(client_module, "get_client", lambda: _FakeClient())
    monkeypatch.setattr(client_module, "get_collateral_balance_usd", lambda client: 100.0)

    order_manager = main_module.build_order_manager()

    assert isinstance(order_manager, OrderManager)


def test_run_signal_strategy_records_activity_and_a_decision_per_leg(tmp_path):
    journal = DecisionJournal(tmp_path / "journal.db")
    order_manager = OrderManager(
        risk_limits=RiskLimits(max_position_usd=1000.0, max_order_usd=1000.0, max_portfolio_exposure_usd=1000.0),
        dry_run=True,
    )
    strategy = ComplementaryOutcomesSignal(taker_fee_bps=0, min_edge_bps=0)
    market = make_market()
    book_yes = make_book("yes", bid=0.43, ask=0.45)
    book_no = make_book("no", bid=0.46, ask=0.48)  # sum(asks) = 0.93 < $1 -> arb fires
    latest_books = {"yes": book_yes}

    # Only "yes" known so far - context is incomplete, no signal should fire.
    main_module._run_signal_strategy(
        "arb", strategy, market, book_yes, latest_books, order_manager, journal
    )
    assert journal.recent_activity() == []

    latest_books["no"] = book_no
    main_module._run_signal_strategy(
        "arb", strategy, market, book_no, latest_books, order_manager, journal
    )

    activity = journal.recent_activity()
    assert len(activity) == 1
    assert activity[0].kind == "signal"

    decisions = journal.recent_decisions()
    assert len(decisions) == 2  # one per outcome leg
    assert {d.token_id for d in decisions} == {"yes", "no"}
    journal.close()


def test_run_market_making_records_a_quote_and_two_decisions(tmp_path):
    journal = DecisionJournal(tmp_path / "journal.db")
    order_manager = OrderManager(
        risk_limits=RiskLimits(max_position_usd=1000.0, max_order_usd=1000.0, max_portfolio_exposure_usd=1000.0),
        dry_run=True,
    )
    strategy = MarketMakingStrategy(
        position_limits=PositionLimits(max_position=1000.0, max_order_size=1000.0)
    )
    market = make_market(token_ids=["mm"])
    book = make_book("mm", bid=0.48, ask=0.52)

    main_module._run_market_making("mm_strat", strategy, market, book, order_manager, journal)

    activity = journal.recent_activity()
    assert len(activity) == 1
    assert activity[0].kind == "quote"
    assert activity[0].bid_price is not None
    assert activity[0].ask_price is not None

    decisions = journal.recent_decisions()
    assert len(decisions) == 2  # bid + ask
    journal.close()
