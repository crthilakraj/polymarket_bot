"""Base interface that all signal strategies implement.

Not every strategy fits this shape - it models a single directional edge
estimate from one evaluate() call. signals/market_making/ is stateful
(inventory persists across calls) and produces a two-sided quote pair rather
than a single Signal, so it's structured separately instead of subclassing
SignalStrategy. See signals/market_making/strategy.py for why.

TODO: implement further concrete strategies (e.g. momentum) as subclasses of
SignalStrategy, alongside complementary_outcomes.py and news/.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from data.models import MarketMetadata, OrderBook


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class Signal:
    """A scored trade idea produced by a SignalStrategy.

    edge_estimate is the expected profit as a fraction of notional (e.g. 0.02
    means "2 cents of edge per dollar risked"), net of the strategy's own fee
    estimate. token_id is set for single-outcome signals; multi-leg signals
    (e.g. an arb spanning every outcome of a market) leave it None and
    describe each leg in metadata instead.
    """

    edge_estimate: float
    confidence: float
    side: Side
    token_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be in [0, 1], got {self.confidence}")


@dataclass(frozen=True)
class SignalContext:
    """Extra data a strategy may need beyond the single market/order_book it's
    evaluating. order_books maps token_id -> OrderBook for every outcome of
    the market being evaluated (including the one passed separately as
    `order_book`), which cross-outcome strategies like complementary-outcomes
    arb need in order to see the whole market rather than one leg at a time.
    metadata is an open slot for strategy-specific extra input - e.g. the
    signals.news strategies expect metadata["headline"] to carry the
    NewsHeadline a caller has already matched to this market.
    """

    order_books: dict[str, OrderBook] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


class SignalStrategy(ABC):
    """A strategy that scores a market/order book for potential edge."""

    @abstractmethod
    def evaluate(
        self,
        market: MarketMetadata,
        order_book: OrderBook,
        context: SignalContext | None = None,
    ) -> Signal | None:
        """Return a Signal describing the edge found, or None if there's no signal."""
        raise NotImplementedError
