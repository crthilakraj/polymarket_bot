"""Stateful market-making strategy: maintains per-market inventory and quotes
both sides of the book with a spread that widens as time-to-resolution
shrinks, inventory skews away from neutral, and order book volatility rises.

This does NOT implement signals.base.SignalStrategy. That interface models a
single directional edge estimate from one stateless evaluate() call, keyed
off (market, order_book, context). Market making doesn't fit: it must
remember inventory across calls (a fill on one quote changes the next one),
and its output is a two-sided quote pair, not a single Signal(side,
edge_estimate, confidence). Forcing it through evaluate() would mean either
smuggling inventory through SignalContext (making every other strategy's
context type implicitly stateful) or returning half of a market-making
decision as a Signal and inventing a second channel for the other side -
both worse than just giving it its own small interface.
"""

import logging
from datetime import datetime, timezone

from data.models import MarketMetadata, OrderBook
from signals.base import Side
from signals.market_making.models import Inventory, PositionLimits, Quote, QuotePair
from signals.market_making.spread import SpreadParams, compute_half_spread_bps
from signals.market_making.volatility import RollingVolatility

logger = logging.getLogger(__name__)

DEFAULT_QUOTE_SIZE = 10.0
DEFAULT_VOLATILITY_WINDOW = 20


class MarketMakingStrategy:
    """Quotes both sides of a token's book, one instance per bot (state is
    keyed internally by token_id, so a single instance can run many markets)."""

    def __init__(
        self,
        position_limits: PositionLimits,
        spread_params: SpreadParams = SpreadParams(),
        quote_size: float = DEFAULT_QUOTE_SIZE,
        volatility_window: int = DEFAULT_VOLATILITY_WINDOW,
    ) -> None:
        self._position_limits = position_limits
        self._spread_params = spread_params
        self._quote_size = quote_size
        self._volatility_window = volatility_window
        self._inventories: dict[str, Inventory] = {}
        self._volatility: dict[str, RollingVolatility] = {}

    def position(self, token_id: str) -> float:
        return self._inventory_for(token_id).position

    def record_fill(self, token_id: str, side: Side, size: float) -> None:
        """Update inventory after an order fills. The caller (execution/) is
        responsible for actually knowing a fill happened - this strategy has
        no market connection of its own."""
        self._inventory_for(token_id).apply_fill(side, size)

    def quote(
        self,
        market: MarketMetadata,
        order_book: OrderBook,
        now: datetime | None = None,
    ) -> QuotePair:
        """Compute this token's next two-sided quote from the current book,
        this strategy's remembered inventory, and market.end_date."""
        now = now or datetime.now(timezone.utc)
        token_id = order_book.token_id

        mid_price = self._mid_price(order_book)
        if mid_price is None:
            logger.debug("no mid price available for %s, skipping quote", token_id)
            return QuotePair(token_id=token_id, bid=None, ask=None)

        volatility_tracker = self._volatility_for(token_id)
        volatility_tracker.update(mid_price)

        position = self.position(token_id)
        inventory_skew = self._inventory_skew(position)
        hours_remaining = self._hours_remaining(market, now)

        half_spread_bps = compute_half_spread_bps(
            hours_remaining=hours_remaining,
            inventory_skew=inventory_skew,
            normalized_volatility=volatility_tracker.normalized_volatility(),
            params=self._spread_params,
        )
        half_spread = mid_price * (half_spread_bps / 10_000)

        bid_size, ask_size = self._capped_quote_sizes(position)

        bid = Quote(price=mid_price - half_spread, size=bid_size) if bid_size > 0 else None
        ask = Quote(price=mid_price + half_spread, size=ask_size) if ask_size > 0 else None
        return QuotePair(token_id=token_id, bid=bid, ask=ask)

    def _capped_quote_sizes(self, position: float) -> tuple[float, float]:
        """Hard position caps: a side that would push |position| past
        max_position is shrunk (down to zero, at which point it isn't quoted)
        rather than ever exceeding the cap."""
        limits = self._position_limits
        room_to_buy = max(0.0, limits.max_position - position)
        room_to_sell = max(0.0, limits.max_position + position)

        bid_size = min(self._quote_size, room_to_buy, limits.max_order_size)
        ask_size = min(self._quote_size, room_to_sell, limits.max_order_size)
        return bid_size, ask_size

    def _inventory_skew(self, position: float) -> float:
        return max(-1.0, min(1.0, position / self._position_limits.max_position))

    def _inventory_for(self, token_id: str) -> Inventory:
        return self._inventories.setdefault(token_id, Inventory(token_id=token_id))

    def _volatility_for(self, token_id: str) -> RollingVolatility:
        if token_id not in self._volatility:
            self._volatility[token_id] = RollingVolatility(self._volatility_window)
        return self._volatility[token_id]

    @staticmethod
    def _mid_price(order_book: OrderBook) -> float | None:
        best_bid = order_book.best_bid
        best_ask = order_book.best_ask
        if best_bid and best_ask:
            return (best_bid.price + best_ask.price) / 2
        if best_bid:
            return best_bid.price
        if best_ask:
            return best_ask.price
        return None

    @staticmethod
    def _hours_remaining(market: MarketMetadata, now: datetime) -> float | None:
        if market.end_date is None:
            return None
        return (market.end_date - now).total_seconds() / 3600.0
