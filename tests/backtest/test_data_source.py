from datetime import datetime, timezone

from data.models import MarketMetadata, OrderBook, PriceLevel
from data.store import DataStore
from backtest.data_source import HistoricalDataSource, infer_resolution


def make_market(**overrides) -> MarketMetadata:
    defaults = dict(
        condition_id="0xcond",
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
    defaults.update(overrides)
    return MarketMetadata(**defaults)


# --- infer_resolution --------------------------------------------------------------


def test_infer_resolution_none_when_market_not_closed():
    market = make_market(closed=False, outcome_prices=[1.0, 0.0])
    assert infer_resolution(market) is None


def test_infer_resolution_returns_payouts_for_a_clean_binary_resolution():
    market = make_market(closed=True, outcome_prices=[1.0, 0.0])
    result = infer_resolution(market)
    assert result == {"t1": 1.0, "t2": 0.0}


def test_infer_resolution_none_when_prices_are_ambiguous():
    market = make_market(closed=True, outcome_prices=[0.6, 0.4])
    assert infer_resolution(market) is None


def test_infer_resolution_none_when_no_outcome_is_a_clear_winner():
    market = make_market(closed=True, outcome_prices=[0.005, 0.005])
    assert infer_resolution(market) is None


def test_infer_resolution_none_on_length_mismatch():
    market = make_market(closed=True, outcome_prices=[1.0], token_ids=["t1", "t2"])
    assert infer_resolution(market) is None


# --- HistoricalDataSource ------------------------------------------------------------


def test_load_markets_skips_unknown_condition_ids(tmp_path):
    store = DataStore(tmp_path / "test.db")
    store.save_market_metadata(make_market(condition_id="0xa"))
    source = HistoricalDataSource(store)

    markets = source.load_markets(["0xa", "0xb"])

    assert set(markets) == {"0xa"}
    store.close()


def test_replay_events_merges_and_orders_snapshots_across_tokens(tmp_path):
    store = DataStore(tmp_path / "test.db")
    market = make_market(token_ids=["t1", "t2"])
    store.save_market_metadata(market)
    store.save_order_book(
        OrderBook(
            token_id="t2",
            condition_id="0xcond",
            bids=[PriceLevel(0.5, 10)],
            asks=[PriceLevel(0.6, 5)],
            exchange_timestamp=None,
            received_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )
    )
    store.save_order_book(
        OrderBook(
            token_id="t1",
            condition_id="0xcond",
            bids=[PriceLevel(0.4, 10)],
            asks=[PriceLevel(0.5, 5)],
            exchange_timestamp=None,
            received_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )
    source = HistoricalDataSource(store)
    markets = source.load_markets(["0xcond"])

    events = list(source.replay_events(markets))

    assert [e.order_book.token_id for e in events] == ["t1", "t2"]
    assert all(e.market.condition_id == "0xcond" for e in events)
    store.close()
