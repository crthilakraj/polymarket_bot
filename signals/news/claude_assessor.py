"""Calls the Claude API to estimate the probability shift a headline implies for a
market, given a structured prompt and a fixed JSON schema for the response.
"""

import json
import logging

import anthropic

from data.models import MarketMetadata
from signals.news.feed import NewsHeadline

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-4-8"
MAX_TOKENS = 1024

ASSESSMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "relevant": {
            "type": "boolean",
            "description": "Whether the headline has any bearing on how this market resolves.",
        },
        "probability_shift": {
            "type": "number",
            "description": (
                "Estimated change in the probability of the market resolving YES, "
                "as a signed fraction in [-1, 1] (e.g. -0.08 means the news makes "
                "YES 8 percentage points less likely). Calibrate this against the "
                "current market price given below - it's a shift from that price, "
                "not an absolute probability. 0 if not relevant or no clear effect."
            ),
        },
        "confidence": {
            "type": "number",
            "description": "Confidence in this estimate, in [0, 1].",
        },
        "rationale": {
            "type": "string",
            "description": "One or two sentences explaining the estimate.",
        },
    },
    "required": ["relevant", "probability_shift", "confidence", "rationale"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = (
    "You are a probabilistic forecaster assessing how a single news headline should "
    "move the price of a prediction market. Be conservative: most headlines have no "
    "effect on most markets, and even relevant news usually implies a small shift. "
    "Only assign a large probability_shift when the headline is direct, specific "
    "evidence about how the market will resolve. Respond only via the provided schema."
)


class NewsAssessmentRefused(RuntimeError):
    """Raised when Claude declines to produce an assessment (stop_reason == 'refusal')."""


class NewsAssessment:
    __slots__ = ("relevant", "probability_shift", "confidence", "rationale")

    def __init__(self, relevant: bool, probability_shift: float, confidence: float, rationale: str):
        self.relevant = relevant
        self.probability_shift = probability_shift
        self.confidence = confidence
        self.rationale = rationale

    def __repr__(self) -> str:
        return (
            f"NewsAssessment(relevant={self.relevant}, "
            f"probability_shift={self.probability_shift!r}, "
            f"confidence={self.confidence!r})"
        )


def _build_prompt(market: MarketMetadata, headline: NewsHeadline, current_price: float | None) -> str:
    price_line = (
        f"Current market price (implied probability of YES): {current_price:.3f}\n"
        if current_price is not None
        else "Current market price: unknown\n"
    )
    return (
        f"Market question: {market.question}\n"
        f"Market outcomes: {', '.join(market.outcomes)}\n"
        f"{price_line}"
        f"Headline: {headline.text}\n"
        f"Headline source: {headline.source}\n"
        f"Headline published at: {headline.published_at.isoformat()}\n\n"
        "Estimate the probability shift this headline implies for the market above."
    )


class ClaudeNewsAssessor:
    """Wraps the Claude API call that turns (market, headline) into a NewsAssessment."""

    def __init__(self, client: anthropic.Anthropic | None = None, model: str = DEFAULT_MODEL):
        self._client = client or anthropic.Anthropic()
        self._model = model

    def assess(
        self,
        market: MarketMetadata,
        headline: NewsHeadline,
        current_price: float | None = None,
    ) -> NewsAssessment:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=MAX_TOKENS,
            thinking={"type": "adaptive"},
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _build_prompt(market, headline, current_price)}],
            output_config={
                "effort": "medium",
                "format": {"type": "json_schema", "schema": ASSESSMENT_SCHEMA},
            },
        )

        if response.stop_reason == "refusal":
            raise NewsAssessmentRefused(
                f"Claude declined to assess headline {headline.id!r} for market "
                f"{market.condition_id!r}"
            )

        text = next(block.text for block in response.content if block.type == "text")
        data = json.loads(text)
        return NewsAssessment(
            relevant=data["relevant"],
            probability_shift=float(data["probability_shift"]),
            confidence=float(data["confidence"]),
            rationale=data["rationale"],
        )
