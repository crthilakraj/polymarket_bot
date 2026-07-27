"""Reads historical order book snapshots and market metadata from
data.store.DataStore and turns them into a chronologically-ordered replay
stream - the only way backtest/ touches stored data, and the only place it
touches data/ at all. No live API calls anywhere in this module.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime

from data.models import MarketMetadata, OrderBook
from data.store import DataStore


@dataclass(frozen=True)
class ReplayEvent:
    order_book: OrderBook
    market: MarketMetadata


class HistoricalDataSource:
    def __init__(self, store: DataStore):
        self._store = store

    def load_markets(self, condition_ids: list[str]) -> dict[str, MarketMetadata]:
        """Metadata for each condition_id that has any stored - silently
        skips ids with nothing in the store (nothing to replay for them)."""
        markets = {}
        for condition_id in condition_ids:
            market = self._store.get_market_metadata(condition_id)
            if market is not None:
                markets[condition_id] = market
        return markets

    def replay_events(
        self,
        markets: dict[str, MarketMetadata],
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> Iterator[ReplayEvent]:
        """Every order book snapshot for every token in `markets`, in a
        single merged chronological stream (received_at ascending)."""
        token_to_market = {
            token_id: market for market in markets.values() for token_id in market.token_ids
        }
        books = self._store.list_order_book_snapshots(list(token_to_market), start=start, end=end)
        for book in books:
            market = token_to_market.get(book.token_id)
            if market is not None:
                yield ReplayEvent(order_book=book, market=market)


def infer_resolution(market: MarketMetadata) -> dict[str, float] | None:
    """token_id -> terminal payout (1.0 or 0.0), inferred from the market's
    latest known outcome_prices. Only returns a result when the market is
    marked closed AND those prices are unambiguous (every outcome within
    0.01 of 0 or 1, with exactly one winner) - this is an approximation:
    it depends on data collection having kept running past resolution, and
    on Gamma's outcome_prices actually settling to 0/1 for closed markets.
    Returns None otherwise, so the caller marks the position to market
    instead of guessing a resolution."""
    if not market.closed:
        return None
    if len(market.outcome_prices) != len(market.token_ids) or not market.outcome_prices:
        return None
    if not all(price <= 0.01 or price >= 0.99 for price in market.outcome_prices):
        return None
    if sum(1 for price in market.outcome_prices if price >= 0.99) != 1:
        return None
    return {
        token_id: round(price) for token_id, price in zip(market.token_ids, market.outcome_prices)
    }
