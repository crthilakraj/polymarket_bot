"""News-driven edge signal: embedding pre-filter to find relevant markets for a
headline, then a Claude assessment of the probability shift it implies, compared
against the current book price.
"""

import logging

from data.models import MarketMetadata, OrderBook
from signals.base import Side, Signal, SignalContext, SignalStrategy
from signals.news.claude_assessor import ClaudeNewsAssessor, NewsAssessmentRefused
from signals.news.embeddings import Embedder, cosine_similarity
from signals.news.feed import NewsHeadline

logger = logging.getLogger(__name__)

DEFAULT_SIMILARITY_THRESHOLD = 0.5
DEFAULT_MIN_PROBABILITY_SHIFT = 0.03


class NewsEdgeSignal(SignalStrategy):
    """Flags a market when a relevant headline implies a probability shift the
    current book price doesn't reflect.

    Usage: call `relevant_markets()` once per headline against your tracked
    markets (cheap, local, no API call), then call `evaluate()` - with the
    headline attached via `SignalContext.metadata["headline"]` - only for the
    markets it returned. That keeps the expensive Claude call off markets the
    headline obviously has nothing to do with.
    """

    def __init__(
        self,
        embedder: Embedder,
        assessor: ClaudeNewsAssessor,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        min_probability_shift: float = DEFAULT_MIN_PROBABILITY_SHIFT,
    ) -> None:
        self._embedder = embedder
        self._assessor = assessor
        self._similarity_threshold = similarity_threshold
        self._min_probability_shift = min_probability_shift

    def is_relevant(self, headline: NewsHeadline, market: MarketMetadata) -> bool:
        if not market.question:
            return False
        similarity = cosine_similarity(
            self._embedder.embed(headline.text), self._embedder.embed(market.question)
        )
        return similarity >= self._similarity_threshold

    def relevant_markets(
        self, headline: NewsHeadline, markets: list[MarketMetadata]
    ) -> list[MarketMetadata]:
        """Local embedding pre-filter: which of `markets` might this headline affect?"""
        return [market for market in markets if self.is_relevant(headline, market)]

    def evaluate(
        self,
        market: MarketMetadata,
        order_book: OrderBook,
        context: SignalContext | None = None,
    ) -> Signal | None:
        headline = (context.metadata.get("headline") if context else None) or None
        if not isinstance(headline, NewsHeadline):
            return None
        if not self.is_relevant(headline, market):
            return None

        current_price = self._implied_probability(order_book)

        try:
            assessment = self._assessor.assess(market, headline, current_price=current_price)
        except NewsAssessmentRefused:
            logger.warning(
                "Claude declined to assess headline %r for market %r",
                headline.id,
                market.condition_id,
            )
            return None

        if not assessment.relevant:
            return None
        if abs(assessment.probability_shift) < self._min_probability_shift:
            return None

        side = Side.BUY if assessment.probability_shift > 0 else Side.SELL
        return Signal(
            edge_estimate=assessment.probability_shift,
            confidence=assessment.confidence,
            side=side,
            token_id=order_book.token_id,
            metadata={
                "strategy": "news_edge",
                "condition_id": market.condition_id,
                "headline_id": headline.id,
                "headline_source": headline.source,
                "current_price": current_price,
                "rationale": assessment.rationale,
            },
        )

    @staticmethod
    def _implied_probability(order_book: OrderBook) -> float | None:
        best_bid = order_book.best_bid
        best_ask = order_book.best_ask
        if best_bid and best_ask:
            return (best_bid.price + best_ask.price) / 2
        if best_bid:
            return best_bid.price
        if best_ask:
            return best_ask.price
        return None
