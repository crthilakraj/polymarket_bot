import json
from datetime import datetime, timezone

import pytest

from data.models import MarketMetadata
from signals.news.claude_assessor import ClaudeNewsAssessor, NewsAssessmentRefused
from signals.news.feed import NewsHeadline


class FakeBlock:
    def __init__(self, type_, text=None):
        self.type = type_
        self.text = text


class FakeResponse:
    def __init__(self, stop_reason, content):
        self.stop_reason = stop_reason
        self.content = content


class FakeMessages:
    def __init__(self, response):
        self._response = response
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


class FakeClient:
    def __init__(self, response):
        self.messages = FakeMessages(response)


def make_market() -> MarketMetadata:
    return MarketMetadata(
        condition_id="0xcond",
        question_id=None,
        question="Will the bill pass by year end?",
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


def make_headline() -> NewsHeadline:
    return NewsHeadline(
        id="h1",
        text="Senate advances the bill in a surprise vote",
        source="test-wire",
        published_at=datetime.now(timezone.utc),
    )


def test_assess_parses_json_response_into_assessment():
    payload = {
        "relevant": True,
        "probability_shift": 0.12,
        "confidence": 0.8,
        "rationale": "Senate advancement makes passage more likely.",
    }
    response = FakeResponse(
        stop_reason="end_turn", content=[FakeBlock("text", json.dumps(payload))]
    )
    client = FakeClient(response)
    assessor = ClaudeNewsAssessor(client=client, model="claude-opus-4-8")

    result = assessor.assess(make_market(), make_headline(), current_price=0.4)

    assert result.relevant is True
    assert result.probability_shift == pytest.approx(0.12)
    assert result.confidence == pytest.approx(0.8)
    assert "advancement" in result.rationale

    call = client.messages.calls[0]
    assert call["model"] == "claude-opus-4-8"
    prompt = call["messages"][0]["content"]
    assert "Will the bill pass by year end?" in prompt
    assert "Senate advances the bill" in prompt
    assert "0.400" in prompt


def test_assess_raises_on_refusal():
    response = FakeResponse(stop_reason="refusal", content=[])
    client = FakeClient(response)
    assessor = ClaudeNewsAssessor(client=client)

    with pytest.raises(NewsAssessmentRefused):
        assessor.assess(make_market(), make_headline())
