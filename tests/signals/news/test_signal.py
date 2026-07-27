from datetime import datetime, timezone

from data.models import MarketMetadata, OrderBook, PriceLevel
from signals.base import Side, SignalContext
from signals.news.claude_assessor import NewsAssessment, NewsAssessmentRefused
from signals.news.feed import NewsHeadline
from signals.news.signal import NewsEdgeSignal


class FakeEmbedder:
    """Returns whatever vector was registered for that exact text."""

    def __init__(self, vectors: dict[str, list[float]]):
        self._vectors = vectors

    def embed(self, text):
        return self._vectors[text]


class FakeAssessor:
    def __init__(self, assessment=None, exc=None):
        self._assessment = assessment
        self._exc = exc
        self.calls = []

    def assess(self, market, headline, current_price=None):
        self.calls.append((market, headline, current_price))
        if self._exc is not None:
            raise self._exc
        return self._assessment


MARKET_QUESTION = "Will the bill pass?"
RELEVANT_HEADLINE_TEXT = "Senate passes the bill"
IRRELEVANT_HEADLINE_TEXT = "Local bakery wins pie contest"


def make_market() -> MarketMetadata:
    return MarketMetadata(
        condition_id="0xcond",
        question_id=None,
        question=MARKET_QUESTION,
        description=None,
        resolution_source=None,
        category=None,
        end_date=None,
        active=True,
        closed=False,
        outcomes=["Yes", "No"],
        outcome_prices=[0.5, 0.5],
        token_ids=["111", "222"],
    )


def make_headline(text: str) -> NewsHeadline:
    return NewsHeadline(id="h1", text=text, source="test-wire", published_at=datetime.now(timezone.utc))


def make_book() -> OrderBook:
    return OrderBook(
        token_id="111",
        condition_id="0xcond",
        bids=[PriceLevel(0.45, 100)],
        asks=[PriceLevel(0.47, 100)],
        exchange_timestamp=None,
    )


def make_embedder() -> FakeEmbedder:
    return FakeEmbedder(
        {
            MARKET_QUESTION: [1.0, 0.0],
            RELEVANT_HEADLINE_TEXT: [1.0, 0.0],
            IRRELEVANT_HEADLINE_TEXT: [0.0, 1.0],
        }
    )


def test_evaluate_returns_none_without_headline_in_context():
    signal = NewsEdgeSignal(embedder=make_embedder(), assessor=FakeAssessor())

    assert signal.evaluate(make_market(), make_book(), context=None) is None
    assert signal.evaluate(make_market(), make_book(), context=SignalContext()) is None


def test_evaluate_returns_none_when_headline_not_relevant():
    assessor = FakeAssessor()
    signal = NewsEdgeSignal(embedder=make_embedder(), assessor=assessor)
    headline = make_headline(IRRELEVANT_HEADLINE_TEXT)

    result = signal.evaluate(
        make_market(), make_book(), SignalContext(metadata={"headline": headline})
    )

    assert result is None
    assert assessor.calls == []  # never even called Claude


def test_evaluate_returns_buy_signal_for_positive_shift_above_threshold():
    assessment = NewsAssessment(
        relevant=True, probability_shift=0.10, confidence=0.75, rationale="Bill likely to pass."
    )
    assessor = FakeAssessor(assessment=assessment)
    signal = NewsEdgeSignal(
        embedder=make_embedder(), assessor=assessor, min_probability_shift=0.03
    )
    headline = make_headline(RELEVANT_HEADLINE_TEXT)

    result = signal.evaluate(
        make_market(), make_book(), SignalContext(metadata={"headline": headline})
    )

    assert result is not None
    assert result.side is Side.BUY
    assert result.edge_estimate == 0.10
    assert result.confidence == 0.75
    assert result.token_id == "111"
    assert result.metadata["headline_id"] == "h1"
    # current_price passed to the assessor is the book midpoint
    assert assessor.calls[0][2] == (0.45 + 0.47) / 2


def test_evaluate_returns_sell_signal_for_negative_shift():
    assessment = NewsAssessment(
        relevant=True, probability_shift=-0.08, confidence=0.6, rationale="Bill likely to fail."
    )
    signal = NewsEdgeSignal(
        embedder=make_embedder(), assessor=FakeAssessor(assessment=assessment), min_probability_shift=0.03
    )
    headline = make_headline(RELEVANT_HEADLINE_TEXT)

    result = signal.evaluate(
        make_market(), make_book(), SignalContext(metadata={"headline": headline})
    )

    assert result is not None
    assert result.side is Side.SELL


def test_evaluate_returns_none_when_shift_below_threshold():
    assessment = NewsAssessment(
        relevant=True, probability_shift=0.01, confidence=0.9, rationale="Minor effect."
    )
    signal = NewsEdgeSignal(
        embedder=make_embedder(),
        assessor=FakeAssessor(assessment=assessment),
        min_probability_shift=0.03,
    )
    headline = make_headline(RELEVANT_HEADLINE_TEXT)

    result = signal.evaluate(
        make_market(), make_book(), SignalContext(metadata={"headline": headline})
    )

    assert result is None


def test_evaluate_returns_none_when_assessment_marks_not_relevant():
    assessment = NewsAssessment(
        relevant=False, probability_shift=0.5, confidence=0.9, rationale="Actually unrelated."
    )
    signal = NewsEdgeSignal(embedder=make_embedder(), assessor=FakeAssessor(assessment=assessment))
    headline = make_headline(RELEVANT_HEADLINE_TEXT)

    result = signal.evaluate(
        make_market(), make_book(), SignalContext(metadata={"headline": headline})
    )

    assert result is None


def test_evaluate_returns_none_on_refusal():
    signal = NewsEdgeSignal(
        embedder=make_embedder(), assessor=FakeAssessor(exc=NewsAssessmentRefused("declined"))
    )
    headline = make_headline(RELEVANT_HEADLINE_TEXT)

    result = signal.evaluate(
        make_market(), make_book(), SignalContext(metadata={"headline": headline})
    )

    assert result is None


def test_relevant_markets_filters_across_multiple_markets():
    signal = NewsEdgeSignal(embedder=make_embedder(), assessor=FakeAssessor())
    relevant_market = make_market()
    other_market = MarketMetadata(
        condition_id="0xother",
        question_id=None,
        question=None,  # no question text - can't be relevant
        description=None,
        resolution_source=None,
        category=None,
        end_date=None,
        active=True,
        closed=False,
        outcomes=[],
        outcome_prices=[],
        token_ids=[],
    )

    result = signal.relevant_markets(
        make_headline(RELEVANT_HEADLINE_TEXT), [relevant_market, other_market]
    )

    assert result == [relevant_market]
