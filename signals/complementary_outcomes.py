"""Complementary-outcomes stat-arb: for a multi-outcome market, exactly one
outcome resolves YES, so the outcomes' fair prices (implied probabilities)
should sum to $1. This flags the two ways that sum can drift away from $1 by
more than the cost of trading it away:

- sum(best_ask) < 1 - threshold: buying one share of every outcome costs
  less than the $1 it's guaranteed to pay out at resolution.
- sum(best_bid) > 1 + threshold: selling one share of every outcome (e.g.
  after minting a complete set for $1 via the CTF) raises more than $1.

`threshold` is fee-adjusted: fee cost per leg uses Polymarket's real,
per-market fee formula (fee = rate * price**exponent * (1-price)**exponent
per share, from Gamma's `feeSchedule` - see
help.polymarket.com/en/articles/13364478) when a market has one, falling
back to a flat taker_fee_bps placeholder otherwise (e.g. a market fetched
before feeSchedule was tracked). The real formula matters here specifically
because it's asymmetric: fee-as-a-fraction-of-price is highest for cheap/
longshot outcomes and near zero for expensive/favorite ones, which is
exactly the shape of a typical complementary-outcomes pair - a flat rate
misprices both legs of the trade this signal is built around. min_edge_bps
adds a minimum required profit margin above breakeven before the signal
fires, so a deviation that only covers fees but doesn't clear it isn't
flagged.
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
        self.fallback_fee_rate = taker_fee_bps / 10_000
        self.min_edge_rate = min_edge_bps / 10_000

    def _fee_per_share(self, price: float, market: MarketMetadata) -> float:
        """Polymarket's real per-share taker fee when the market has a known
        fee schedule (fee = rate * price**exponent * (1-price)**exponent),
        otherwise the flat placeholder rate this signal was constructed
        with. exponent defaults to 1 if the schedule reports a rate but no
        exponent - matches every fee schedule observed live so far."""
        if market.fee_rate is not None:
            exponent = market.fee_exponent if market.fee_exponent is not None else 1.0
            return market.fee_rate * (price**exponent) * ((1.0 - price) ** exponent)
        return price * self.fallback_fee_rate

    def fee_rate_for(self, price: float, market: MarketMetadata) -> float:
        """As a rate (fraction of notional) rather than a dollar amount, for
        backtest/engine.py's Portfolio.apply_fill(fee_rate=...) - which
        expects a rate so it can fold the fee into effective_price on
        whatever size actually fills, not just the size this signal saw."""
        return self._fee_per_share(price, market) / price if price else 0.0

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
        best_ask_sizes: dict[str, float] = {}
        best_bids: dict[str, float] = {}
        best_bid_sizes: dict[str, float] = {}
        for token_id in market.token_ids:
            book = order_books[token_id]
            best_ask = book.best_ask
            best_bid = book.best_bid
            if best_ask is None or best_bid is None:
                return None  # one-sided or empty book - can't price this leg
            best_asks[token_id] = best_ask.price
            best_ask_sizes[token_id] = best_ask.size
            best_bids[token_id] = best_bid.price
            best_bid_sizes[token_id] = best_bid.size

        buy_signal = self._check_buy_complete_set(market, best_asks, best_ask_sizes)
        if buy_signal is not None:
            return buy_signal

        return self._check_sell_complete_set(market, best_bids, best_bid_sizes)

    def _check_buy_complete_set(
        self, market: MarketMetadata, best_asks: dict[str, float], best_ask_sizes: dict[str, float]
    ) -> Signal | None:
        sum_ask = sum(best_asks.values())
        fee_cost = sum(self._fee_per_share(price, market) for price in best_asks.values())
        edge = 1.0 - sum_ask - fee_cost
        if edge <= self.min_edge_rate:
            return None
        # The quoted price only holds for the size actually resting at the
        # top of book - sizing past that on any single leg would walk the
        # book to worse prices and could erase the edge this signal just
        # computed. Cap at the thinnest leg's available size; OrderManager
        # additionally caps by risk limits, so the final size is whichever
        # is smaller.
        max_shares = min(best_ask_sizes.values())
        return Signal(
            edge_estimate=edge,
            confidence=self._confidence(edge),
            side=Side.BUY,
            metadata={
                "strategy": "complementary_outcomes",
                "condition_id": market.condition_id,
                "sum_probability": sum_ask,
                "fee_cost": fee_cost,
                "max_shares": max_shares,
                "legs": [
                    {"token_id": token_id, "side": Side.BUY, "price": price}
                    for token_id, price in best_asks.items()
                ],
            },
        )

    def _check_sell_complete_set(
        self, market: MarketMetadata, best_bids: dict[str, float], best_bid_sizes: dict[str, float]
    ) -> Signal | None:
        sum_bid = sum(best_bids.values())
        fee_cost = sum(self._fee_per_share(price, market) for price in best_bids.values())
        edge = sum_bid - 1.0 - fee_cost
        if edge <= self.min_edge_rate:
            return None
        max_shares = min(best_bid_sizes.values())
        return Signal(
            edge_estimate=edge,
            confidence=self._confidence(edge),
            side=Side.SELL,
            metadata={
                "strategy": "complementary_outcomes",
                "condition_id": market.condition_id,
                "sum_probability": sum_bid,
                "fee_cost": fee_cost,
                "max_shares": max_shares,
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
