from datetime import datetime, timezone

from data.models import MarketMetadata, OrderBook, PriceLevel
from data.store import DataStore


def test_save_and_count_order_book(tmp_path):
    store = DataStore(tmp_path / "test.db")
    book = OrderBook(
        token_id="token-1",
        condition_id="0xcond",
        bids=[PriceLevel(0.5, 10)],
        asks=[PriceLevel(0.6, 5)],
        exchange_timestamp=datetime.now(timezone.utc),
        book_hash="0xhash",
    )

    store.save_order_book(book)

    assert store.count_order_book_snapshots() == 1
    store.close()


def test_save_market_metadata_upserts(tmp_path):
    store = DataStore(tmp_path / "test.db")
    market = MarketMetadata(
        condition_id="0xcond",
        question_id="0xq",
        question="Will X happen?",
        description="desc",
        resolution_source="source",
        category="Politics",
        end_date=datetime.now(timezone.utc),
        active=True,
        closed=False,
        outcomes=["Yes", "No"],
        outcome_prices=[0.5, 0.5],
        token_ids=["1", "2"],
    )

    store.save_market_metadata(market)
    store.save_market_metadata(market)  # same condition_id - should upsert, not duplicate

    cursor = store._conn.execute("SELECT COUNT(*) FROM market_metadata")
    assert cursor.fetchone()[0] == 1
    store.close()


def test_get_market_metadata_roundtrips(tmp_path):
    store = DataStore(tmp_path / "test.db")
    market = MarketMetadata(
        condition_id="0xcond",
        question_id="0xq",
        question="Will X happen?",
        description="desc",
        resolution_source="source",
        category="Politics",
        end_date=datetime(2026, 6, 1, tzinfo=timezone.utc),
        active=True,
        closed=True,
        outcomes=["Yes", "No"],
        outcome_prices=[1.0, 0.0],
        token_ids=["1", "2"],
    )
    store.save_market_metadata(market)

    fetched = store.get_market_metadata("0xcond")

    assert fetched.condition_id == "0xcond"
    assert fetched.question == "Will X happen?"
    assert fetched.closed is True
    assert fetched.outcome_prices == [1.0, 0.0]
    assert fetched.token_ids == ["1", "2"]
    assert fetched.end_date == datetime(2026, 6, 1, tzinfo=timezone.utc)
    store.close()


def test_get_market_metadata_returns_none_when_missing(tmp_path):
    store = DataStore(tmp_path / "test.db")
    assert store.get_market_metadata("0xnope") is None
    store.close()


def test_list_order_book_snapshots_returns_chronological_order(tmp_path):
    store = DataStore(tmp_path / "test.db")
    for i, ts in enumerate([3, 1, 2]):
        store.save_order_book(
            OrderBook(
                token_id="t1",
                condition_id="0xcond",
                bids=[PriceLevel(0.5, 10)],
                asks=[PriceLevel(0.6, 5)],
                exchange_timestamp=None,
                received_at=datetime(2026, 1, ts, tzinfo=timezone.utc),
            )
        )

    snapshots = store.list_order_book_snapshots(["t1"])

    assert [s.received_at.day for s in snapshots] == [1, 2, 3]
    store.close()


def test_list_order_book_snapshots_filters_by_token_and_date_range(tmp_path):
    store = DataStore(tmp_path / "test.db")
    for token_id in ("t1", "t2"):
        for day in (1, 2, 3):
            store.save_order_book(
                OrderBook(
                    token_id=token_id,
                    condition_id="0xcond",
                    bids=[PriceLevel(0.5, 10)],
                    asks=[PriceLevel(0.6, 5)],
                    exchange_timestamp=None,
                    received_at=datetime(2026, 1, day, tzinfo=timezone.utc),
                )
            )

    only_t1 = store.list_order_book_snapshots(["t1"])
    assert len(only_t1) == 3
    assert all(s.token_id == "t1" for s in only_t1)

    ranged = store.list_order_book_snapshots(
        ["t1", "t2"],
        start=datetime(2026, 1, 2, tzinfo=timezone.utc),
        end=datetime(2026, 1, 2, 23, 59, tzinfo=timezone.utc),
    )
    assert len(ranged) == 2
    assert all(s.received_at.day == 2 for s in ranged)
    store.close()


def test_list_order_book_snapshots_empty_token_list_short_circuits(tmp_path):
    store = DataStore(tmp_path / "test.db")
    assert store.list_order_book_snapshots([]) == []
    store.close()


def test_get_latest_order_book_returns_the_most_recent_snapshot(tmp_path):
    store = DataStore(tmp_path / "test.db")
    for day, ask in ((1, 0.50), (2, 0.55), (3, 0.60)):
        store.save_order_book(
            OrderBook(
                token_id="t1",
                condition_id="0xcond",
                bids=[PriceLevel(0.40, 10)],
                asks=[PriceLevel(ask, 10)],
                exchange_timestamp=None,
                received_at=datetime(2026, 1, day, tzinfo=timezone.utc),
            )
        )

    latest = store.get_latest_order_book("t1")

    assert latest.received_at.day == 3
    assert latest.best_ask.price == 0.60
    store.close()


def test_get_latest_order_book_returns_none_when_missing(tmp_path):
    store = DataStore(tmp_path / "test.db")
    assert store.get_latest_order_book("nope") is None
    store.close()
