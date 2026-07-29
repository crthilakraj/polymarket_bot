import json
from datetime import datetime, timezone

from data.gamma_client import GammaClient, _to_market_metadata


class FakeResponse:
    def __init__(self, status_code, json_data=None, headers=None):
        self.status_code = status_code
        self._json_data = json_data
        self.headers = headers or {}

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttpClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, params=None):
        self.calls.append((url, params))
        return self._responses.pop(0)


SAMPLE_MARKET = {
    "conditionId": "0xabc",
    "questionID": "0xq",
    "question": "Will X happen?",
    "description": "Resolves YES if X happens by end date.",
    "resolutionSource": "https://example.com",
    "category": "Politics",
    "endDate": "2026-12-31T00:00:00Z",
    "active": True,
    "closed": False,
    "outcomes": json.dumps(["Yes", "No"]),
    "outcomePrices": json.dumps(["0.6", "0.4"]),
    "clobTokenIds": json.dumps(["111", "222"]),
}


def test_to_market_metadata_parses_json_encoded_fields():
    market = _to_market_metadata(SAMPLE_MARKET)

    assert market.condition_id == "0xabc"
    assert market.outcomes == ["Yes", "No"]
    assert market.outcome_prices == [0.6, 0.4]
    assert market.token_ids == ["111", "222"]
    assert market.end_date == datetime(2026, 12, 31, tzinfo=timezone.utc)


def test_get_markets_by_condition_ids_returns_parsed_markets():
    closed_market = {**SAMPLE_MARKET, "conditionId": "0xdef", "closed": True}
    fake_client = FakeHttpClient(
        [
            FakeResponse(200, json_data=[SAMPLE_MARKET]),
            FakeResponse(200, json_data=[closed_market]),
        ]
    )
    client = GammaClient(base_url="https://gamma.test", client=fake_client)

    markets = client.get_markets_by_condition_ids(["0xabc", "0xdef"])

    # Queries both closed=false and closed=true since Gamma silently drops
    # already-closed markets from a condition_ids-only query - a resolved
    # market must not be invisible to a caller polling for resolutions.
    assert len(markets) == 2
    assert {m.condition_id for m in markets} == {"0xabc", "0xdef"}
    assert fake_client.calls[0][1] == {"condition_ids": ["0xabc", "0xdef"], "closed": "false"}
    assert fake_client.calls[1][1] == {"condition_ids": ["0xabc", "0xdef"], "closed": "true"}


def test_get_markets_by_condition_ids_empty_list_short_circuits():
    fake_client = FakeHttpClient([])
    client = GammaClient(base_url="https://gamma.test", client=fake_client)

    assert client.get_markets_by_condition_ids([]) == []
    assert fake_client.calls == []


def test_retries_on_429_then_succeeds(monkeypatch):
    monkeypatch.setattr("data.gamma_client.time.sleep", lambda _: None)
    fake_client = FakeHttpClient(
        [
            FakeResponse(429),
            FakeResponse(200, json_data=[SAMPLE_MARKET]),
            FakeResponse(200, json_data=[]),
        ]
    )
    client = GammaClient(base_url="https://gamma.test", client=fake_client)

    markets = client.get_markets_by_condition_ids(["0xabc"])

    assert len(markets) == 1
    assert len(fake_client.calls) == 3


SAMPLE_EVENT = {
    "id": "1",
    "title": "Some event",
    "tags": [{"label": "Politics"}],
    "markets": [SAMPLE_MARKET],
}


def test_get_active_events_stops_on_a_short_page():
    fake_client = FakeHttpClient([FakeResponse(200, json_data=[SAMPLE_EVENT])])
    client = GammaClient(base_url="https://gamma.test", client=fake_client)

    events = client.get_active_events(page_limit=100)

    assert events == [SAMPLE_EVENT]
    assert len(fake_client.calls) == 1
    _, params = fake_client.calls[0]
    assert params == {"active": True, "closed": False, "limit": 100, "offset": 0}


def test_get_active_events_paginates_until_a_short_page():
    full_page = [SAMPLE_EVENT, SAMPLE_EVENT]
    short_page = [SAMPLE_EVENT]
    fake_client = FakeHttpClient(
        [FakeResponse(200, json_data=full_page), FakeResponse(200, json_data=short_page)]
    )
    client = GammaClient(base_url="https://gamma.test", client=fake_client)

    events = client.get_active_events(page_limit=2)

    assert len(events) == 3
    assert len(fake_client.calls) == 2
    assert fake_client.calls[0][1]["offset"] == 0
    assert fake_client.calls[1][1]["offset"] == 2


def test_get_active_events_stops_on_empty_page():
    fake_client = FakeHttpClient([FakeResponse(200, json_data=[])])
    client = GammaClient(base_url="https://gamma.test", client=fake_client)

    assert client.get_active_events() == []
    assert len(fake_client.calls) == 1


def test_get_active_events_respects_max_markets():
    # Each page has 2 events * 1 market = 2 markets; max_markets=2 should stop after page 1.
    fake_client = FakeHttpClient(
        [
            FakeResponse(200, json_data=[SAMPLE_EVENT, SAMPLE_EVENT]),
            FakeResponse(200, json_data=[SAMPLE_EVENT, SAMPLE_EVENT]),
        ]
    )
    client = GammaClient(base_url="https://gamma.test", client=fake_client)

    events = client.get_active_events(page_limit=2, max_markets=2)

    assert len(events) == 2
    assert len(fake_client.calls) == 1
