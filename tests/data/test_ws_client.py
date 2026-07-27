import asyncio

import pytest

from data.ws_client import ClobWebSocketClient


def make_client(on_update=None):
    updates = []

    def default_on_update(book):
        updates.append(book)

    client = ClobWebSocketClient(
        token_ids=["token-1"],
        on_book_update=on_update or default_on_update,
    )
    return client, updates


def test_requires_at_least_one_token_id():
    with pytest.raises(ValueError):
        ClobWebSocketClient(token_ids=[], on_book_update=lambda book: None)


def test_apply_snapshot_normalizes_book():
    client, updates = make_client()
    event = {
        "event_type": "book",
        "asset_id": "token-1",
        "market": "0xcond",
        "bids": [{"price": ".48", "size": "30"}],
        "asks": [{"price": ".52", "size": "25"}],
        "timestamp": "1700000000000",
        "hash": "0xhash",
    }

    asyncio.run(client._handle_event(event))

    assert len(updates) == 1
    book = updates[0]
    assert book.token_id == "token-1"
    assert book.best_bid.price == 0.48
    assert book.best_ask.price == 0.52
    assert book.book_hash == "0xhash"


def test_apply_delta_updates_existing_level():
    client, updates = make_client()
    asyncio.run(
        client._handle_event(
            {
                "event_type": "book",
                "asset_id": "token-1",
                "bids": [{"price": "0.5", "size": "10"}],
                "asks": [{"price": "0.6", "size": "10"}],
                "timestamp": "1700000000000",
                "hash": "0xhash1",
            }
        )
    )
    asyncio.run(
        client._handle_event(
            {
                "event_type": "price_change",
                "timestamp": "1700000001000",
                "price_changes": [
                    {
                        "asset_id": "token-1",
                        "price": "0.5",
                        "size": "0",
                        "side": "BUY",
                        "hash": "0xhash2",
                    },
                    {
                        "asset_id": "token-1",
                        "price": "0.55",
                        "size": "5",
                        "side": "BUY",
                        "hash": "0xhash2",
                    },
                ],
            }
        )
    )

    book = updates[-1]
    prices = {level.price for level in book.bids}
    assert 0.5 not in prices
    assert 0.55 in prices


def test_delta_ignored_before_snapshot():
    client, updates = make_client()
    asyncio.run(
        client._handle_event(
            {
                "event_type": "price_change",
                "price_changes": [
                    {"asset_id": "token-1", "price": "0.5", "size": "5", "side": "BUY"}
                ],
            }
        )
    )
    assert updates == []


def test_pong_text_is_ignored():
    client, updates = make_client()
    asyncio.run(client._handle_message("PONG"))
    assert updates == []


def test_async_on_book_update_is_awaited():
    events_seen = []

    async def on_update(book):
        events_seen.append(book)

    client, _ = make_client(on_update=on_update)
    event = {
        "event_type": "book",
        "asset_id": "token-1",
        "bids": [],
        "asks": [],
        "timestamp": "1700000000000",
        "hash": "0xhash",
    }
    asyncio.run(client._handle_event(event))
    assert len(events_seen) == 1
