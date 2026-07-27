"""Complementary-outcomes stat-arb: for a multi-outcome market, exactly one
outcome resolves YES, so the outcomes' fair prices (implied probabilities)
should sum to $1. This flags the two ways that sum can drift away from $1 by
more than the cost of trading it away:

- sum(best_ask) < 1 - threshold: buying one share of every outcome costs
  less than the $1 it's guaranteed to pay out at resolution.
- sum(best_bid) > 1 + threshold: selling one share of every outcome (e.g.
  after minting a complete set for $1 via the CTF) raises more than $1.

`threshold` is fee-adjusted: it's derived from taker_fee_bps (the estimated
per-leg taker fee, as a fraction of each leg's notional) plus min_edge_bps,
a minimum required profit margin above breakeven before the signal fires -
so a deviation that only covers fees but doesn't clear it isn't flagged.
"""

from data.models import MarketMetadata, OrderBook
from signals.base import Side, Signal, SignalContext, SignalStrategy

DEFAULT_TAKER_FEE_BPS = 200.0  # 2% per leg - conservative placeholder, tune per venue/market
DEFAULT_MIN_EDGE_BPS = 50.0  # require 0.5% of expected profit above fees to fire


class ComplementaryOutcomesSignal(SignalStrategy):
    """Flags mispricing in the sum of a multi-outcome market's best bid/ask prices."""

    def __init__(
        self,
        taker_fee_bps: float = DEFAULT_TAKER_FEE_BPS,
        min_edge_bps: float = DEFAULT_MIN_EDGE_BPS,
    ) -> None:
        self.taker_fee_rate = taker_fee_bps / 10_000
        self.min_edge_rate = min_edge_bps / 10_000

    def evaluate(
        self,
        market: MarketMetadata,
        order_book: OrderBook,
        context: SignalContext | None = None,
    ) -> Signal | None:
        context = context or SignalContext()
        order_books = context.order_books

        if len(market.token_ids) < 2:
            return None  # not a multi-outcome market
        if set(market.token_ids) - set(order_books):
            return None  # missing a book for at least one outcome - can't sum safely

        best_asks: dict[str, float] = {}
        best_bids: dict[str, float] = {}
        for token_id in market.token_ids:
            book = order_books[token_id]
            best_ask = book.best_ask
            best_bid = book.best_bid
            if best_ask is None or best_bid is None:
                return None  # one-sided or empty book - can't price this leg
            best_asks[token_id] = best_ask.price
            best_bids[token_id] = best_bid.price

        buy_signal = self._check_buy_complete_set(market, best_asks)
        if buy_signal is not None:
            return buy_signal

        return self._check_sell_complete_set(market, best_bids)

    def _check_buy_complete_set(
        self, market: MarketMetadata, best_asks: dict[str, float]
    ) -> Signal | None:
        sum_ask = sum(best_asks.values())
        fee_cost = sum(price * self.taker_fee_rate for price in best_asks.values())
        edge = 1.0 - sum_ask - fee_cost
        if edge <= self.min_edge_rate:
            return None
        return Signal(
            edge_estimate=edge,
            confidence=self._confidence(edge),
            side=Side.BUY,
            metadata={
                "strategy": "complementary_outcomes",
                "condition_id": market.condition_id,
                "sum_probability": sum_ask,
                "fee_cost": fee_cost,
                "legs": [
                    {"token_id": token_id, "side": Side.BUY, "price": price}
                    for token_id, price in best_asks.items()
                ],
            },
        )

    def _check_sell_complete_set(
        self, market: MarketMetadata, best_bids: dict[str, float]
    ) -> Signal | None:
        sum_bid = sum(best_bids.values())
        fee_cost = sum(price * self.taker_fee_rate for price in best_bids.values())
        edge = sum_bid - 1.0 - fee_cost
        if edge <= self.min_edge_rate:
            return None
        return Signal(
            edge_estimate=edge,
            confidence=self._confidence(edge),
            side=Side.SELL,
            metadata={
                "strategy": "complementary_outcomes",
                "condition_id": market.condition_id,
                "sum_probability": sum_bid,
                "fee_cost": fee_cost,
                "legs": [
                    {"token_id": token_id, "side": Side.SELL, "price": price}
                    for token_id, price in best_bids.items()
                ],
            },
        )

    def _confidence(self, edge: float) -> float:
        """Scales from 0 at breakeven (edge == min_edge_rate) to 1 once edge
        is 4x the required minimum - a simple, monotonic placeholder until
        this is calibrated against realized fill quality."""
        if self.min_edge_rate <= 0:
            return 1.0 if edge > 0 else 0.0
        return max(0.0, min(1.0, edge / (4 * self.min_edge_rate)))
