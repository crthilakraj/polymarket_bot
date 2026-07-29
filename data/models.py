"""Normalized data shapes shared across the data layer."""

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class PriceLevel:
    price: float
    size: float


@dataclass
class OrderBook:
    """A normalized order book snapshot for a single CLOB token (outcome)."""

    token_id: str
    condition_id: str | None
    bids: list[PriceLevel]
    asks: list[PriceLevel]
    exchange_timestamp: datetime | None
    book_hash: str | None = None
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def best_bid(self) -> PriceLevel | None:
        return max(self.bids, key=lambda level: level.price, default=None)

    @property
    def best_ask(self) -> PriceLevel | None:
        return min(self.asks, key=lambda level: level.price, default=None)

    @property
    def mid_price(self) -> float | None:
        bid = self.best_bid
        ask = self.best_ask
        if bid and ask:
            return (bid.price + ask.price) / 2
        if bid:
            return bid.price
        if ask:
            return ask.price
        return None


@dataclass
class MarketMetadata:
    """Market metadata as returned by the Gamma API."""

    condition_id: str
    question_id: str | None
    question: str | None
    description: str | None
    resolution_source: str | None
    category: str | None
    end_date: datetime | None
    active: bool | None
    closed: bool | None
    outcomes: list[str]
    outcome_prices: list[float]
    token_ids: list[str]
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Polymarket's real taker fee: fee = rate * price**exponent * (1-price)**exponent
    # per share, from Gamma's `feeSchedule` (see help.polymarket.com/en/articles/13364478).
    # None when Gamma didn't report a fee schedule for this market (e.g. an
    # older cached row fetched before this field was tracked) - callers should
    # fall back to a flat placeholder rate in that case, not assume fee-free.
    fee_rate: float | None = None
    fee_exponent: float | None = None
