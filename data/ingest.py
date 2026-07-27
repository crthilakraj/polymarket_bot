"""Wires the Gamma client, CLOB WebSocket client, and DataStore together:
resolves condition_ids to token_ids, streams+persists order book updates, and
periodically refreshes market metadata.
"""

import asyncio
import logging

from data.gamma_client import GammaClient
from data.models import MarketMetadata, OrderBook
from data.store import DataStore
from data.ws_client import ClobWebSocketClient

logger = logging.getLogger(__name__)


def _resolve_token_ids(markets: list[MarketMetadata]) -> dict[str, str]:
    """Map token_id -> condition_id for every outcome across the given markets."""
    return {token_id: market.condition_id for market in markets for token_id in market.token_ids}


async def run(
    condition_ids: list[str],
    db_path: str,
    gamma_refresh_interval: float = 300.0,
) -> None:
    if not condition_ids:
        raise ValueError("condition_ids must be non-empty")

    store = DataStore(db_path)
    gamma = GammaClient()

    try:
        markets = await asyncio.to_thread(gamma.get_markets_by_condition_ids, condition_ids)
        if not markets:
            raise RuntimeError(
                f"Gamma returned no markets for condition_ids={condition_ids} - check the ids"
            )
        for market in markets:
            store.save_market_metadata(market)
            logger.info(
                "tracking market %s: %r (%d outcomes)",
                market.condition_id,
                market.question,
                len(market.token_ids),
            )

        token_to_condition = _resolve_token_ids(markets)

        def on_book_update(book: OrderBook) -> None:
            book.condition_id = token_to_condition.get(book.token_id)
            store.save_order_book(book)
            logger.info(
                "book update token=%s best_bid=%s best_ask=%s levels=%d/%d",
                book.token_id,
                book.best_bid,
                book.best_ask,
                len(book.bids),
                len(book.asks),
            )

        ws_client = ClobWebSocketClient(
            token_ids=list(token_to_condition), on_book_update=on_book_update
        )

        async def refresh_metadata_loop() -> None:
            while True:
                await asyncio.sleep(gamma_refresh_interval)
                try:
                    refreshed = await asyncio.to_thread(
                        gamma.get_markets_by_condition_ids, condition_ids
                    )
                    for market in refreshed:
                        store.save_market_metadata(market)
                    logger.info("refreshed metadata for %d market(s)", len(refreshed))
                except Exception:
                    logger.exception("gamma metadata refresh failed, will retry next interval")

        await asyncio.gather(ws_client.run(), refresh_metadata_loop())
    finally:
        gamma.close()
        store.close()
