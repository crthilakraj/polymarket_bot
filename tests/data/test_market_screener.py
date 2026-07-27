import json
from datetime import datetime, timedelta, timezone

import pytest

from data.market_screener import (
    MarketCandidate,
    ScreenerCriteria,
    _parse_market,
    fetch_market_candidates,
    passes_criteria,
    rank_markets,
    write_tracked_markets,
)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_candidate(**overrides) -> MarketCandidate:
    defaults = dict(
        question="Will X happen?",
        condition_id="0xcond",
        category=["Politics"],
        volume_24h=10_000.0,
        liquidity=5_000.0,
        days_to_resolution=10.0,
        outcome_count=2,
    )
    defaults.update(overrides)
    return MarketCandidate(**defaults)


# --- passes_criteria ---------------------------------------------------------------


def test_passes_with_default_criteria():
    assert passes_criteria(make_candidate(), ScreenerCriteria())


def test_rejects_below_min_volume():
    criteria = ScreenerCriteria(min_volume_24h=20_000.0)
    assert not passes_criteria(make_candidate(volume_24h=10_000.0), criteria)


def test_rejects_below_min_liquidity():
    criteria = ScreenerCriteria(min_liquidity=10_000.0)
    assert not passes_criteria(make_candidate(liquidity=5_000.0), criteria)


def test_days_to_resolution_within_range_passes():
    criteria = ScreenerCriteria(min_days_to_resolution=1.0, max_days_to_resolution=30.0)
    assert passes_criteria(make_candidate(days_to_resolution=10.0), criteria)


def test_days_to_resolution_outside_range_fails():
    criteria = ScreenerCriteria(min_days_to_resolution=1.0, max_days_to_resolution=30.0)
    assert not passes_criteria(make_candidate(days_to_resolution=45.0), criteria)
    assert not passes_criteria(make_candidate(days_to_resolution=0.5), criteria)


def test_unknown_days_to_resolution_fails_when_a_bound_is_set():
    criteria = ScreenerCriteria(max_days_to_resolution=30.0)
    assert not passes_criteria(make_candidate(days_to_resolution=None), criteria)


def test_unknown_days_to_resolution_passes_when_no_bound_is_set():
    criteria = ScreenerCriteria()
    assert passes_criteria(make_candidate(days_to_resolution=None), criteria)


def test_outcome_count_bounds():
    binary_only = ScreenerCriteria(max_outcome_count=2)
    assert passes_criteria(make_candidate(outcome_count=2), binary_only)
    assert not passes_criteria(make_candidate(outcome_count=5), binary_only)

    multi_only = ScreenerCriteria(min_outcome_count=3)
    assert not passes_criteria(make_candidate(outcome_count=2), multi_only)
    assert passes_criteria(make_candidate(outcome_count=5), multi_only)


def test_category_matches_case_insensitively_against_tags():
    criteria = ScreenerCriteria(category="politics")
    assert passes_criteria(make_candidate(category=["Politics", "US Elections"]), criteria)
    assert not passes_criteria(make_candidate(category=["Sports"]), criteria)


def test_category_none_means_no_filtering():
    assert passes_criteria(make_candidate(category=[]), ScreenerCriteria(category=None))


# --- rank_markets --------------------------------------------------------------------


def test_rank_markets_filters_and_sorts_by_volume_descending():
    candidates = [
        make_candidate(condition_id="low", volume_24h=100.0),
        make_candidate(condition_id="high", volume_24h=9_000.0),
        make_candidate(condition_id="mid", volume_24h=500.0),
    ]

    ranked = rank_markets(candidates, ScreenerCriteria())

    assert [c.condition_id for c in ranked] == ["high", "mid", "low"]


def test_rank_markets_respects_limit():
    candidates = [make_candidate(condition_id=str(i), volume_24h=float(i)) for i in range(10)]

    ranked = rank_markets(candidates, ScreenerCriteria(), limit=3)

    assert len(ranked) == 3
    assert ranked[0].condition_id == "9"  # highest volume first


def test_rank_markets_excludes_candidates_failing_criteria():
    candidates = [
        make_candidate(condition_id="a", volume_24h=100.0),
        make_candidate(condition_id="b", volume_24h=9_000.0),
    ]

    ranked = rank_markets(candidates, ScreenerCriteria(min_volume_24h=1_000.0))

    assert [c.condition_id for c in ranked] == ["b"]


# --- _parse_market -------------------------------------------------------------------


def make_raw_market(**overrides) -> dict:
    defaults = dict(
        conditionId="0xcond",
        question="Will X happen?",
        outcomes='["Yes", "No"]',
        volume24hr=1234.5,
        liquidityNum=678.9,
        endDate="2026-01-11T00:00:00Z",
    )
    defaults.update(overrides)
    return defaults


def test_parse_market_happy_path():
    candidate = _parse_market(make_raw_market(), tags=["Politics"], now=NOW)

    assert candidate.question == "Will X happen?"
    assert candidate.condition_id == "0xcond"
    assert candidate.outcome_count == 2
    assert candidate.volume_24h == pytest.approx(1234.5)
    assert candidate.liquidity == pytest.approx(678.9)
    assert candidate.days_to_resolution == pytest.approx(10.0)
    assert candidate.category == ["Politics"]


def test_parse_market_returns_none_without_condition_id():
    raw = make_raw_market()
    del raw["conditionId"]
    assert _parse_market(raw, tags=[], now=NOW) is None


def test_parse_market_returns_none_without_question():
    raw = make_raw_market()
    del raw["question"]
    assert _parse_market(raw, tags=[], now=NOW) is None


def test_parse_market_handles_malformed_outcomes_json():
    raw = make_raw_market(outcomes="not json")
    candidate = _parse_market(raw, tags=[], now=NOW)
    assert candidate.outcome_count == 0


def test_parse_market_handles_missing_end_date():
    raw = make_raw_market()
    del raw["endDate"]
    candidate = _parse_market(raw, tags=[], now=NOW)
    assert candidate.days_to_resolution is None


def test_parse_market_handles_malformed_end_date():
    raw = make_raw_market(endDate="not-a-date")
    candidate = _parse_market(raw, tags=[], now=NOW)
    assert candidate.days_to_resolution is None


def test_parse_market_defaults_missing_volume_and_liquidity_to_zero():
    raw = make_raw_market()
    del raw["volume24hr"]
    del raw["liquidityNum"]
    candidate = _parse_market(raw, tags=[], now=NOW)
    assert candidate.volume_24h == 0.0
    assert candidate.liquidity == 0.0


# --- fetch_market_candidates -----------------------------------------------------------


class FakeGammaClient:
    def __init__(self, events: list[dict]):
        self._events = events

    def get_active_events(self, page_limit=100, max_markets=2000):
        return self._events


def test_fetch_market_candidates_aggregates_markets_and_tags_across_events():
    events = [
        {
            "tags": [{"label": "Politics"}, {"label": "US Elections"}],
            "markets": [make_raw_market(conditionId="0xa", question="A?")],
        },
        {
            "tags": [{"label": "Sports"}],
            "markets": [
                make_raw_market(conditionId="0xb", question="B?"),
                make_raw_market(conditionId="0xc", question="C?"),
            ],
        },
    ]
    gamma = FakeGammaClient(events)

    candidates = fetch_market_candidates(gamma, now=NOW)

    assert {c.condition_id for c in candidates} == {"0xa", "0xb", "0xc"}
    by_id = {c.condition_id: c for c in candidates}
    assert by_id["0xa"].category == ["Politics", "US Elections"]
    assert by_id["0xb"].category == ["Sports"]


def test_fetch_market_candidates_skips_unparseable_markets():
    events = [
        {
            "tags": [],
            "markets": [make_raw_market(conditionId="0xgood"), {"question": "no condition id"}],
        }
    ]
    gamma = FakeGammaClient(events)

    candidates = fetch_market_candidates(gamma, now=NOW)

    assert len(candidates) == 1
    assert candidates[0].condition_id == "0xgood"


# --- write_tracked_markets -------------------------------------------------------------


def test_write_tracked_markets_creates_a_new_file(tmp_path):
    path = tmp_path / "tracked_markets.json"
    candidates = [make_candidate(condition_id="0xa", question="A?")]

    merged = write_tracked_markets(candidates, str(path))

    assert merged == [{"condition_id": "0xa", "question": "A?"}]
    assert json.loads(path.read_text()) == merged


def test_write_tracked_markets_merges_and_dedupes_with_existing_file(tmp_path):
    path = tmp_path / "tracked_markets.json"
    path.write_text(json.dumps([{"condition_id": "0xa", "question": "A?"}]))
    candidates = [
        make_candidate(condition_id="0xa", question="A? (renamed)"),  # dup - existing wins
        make_candidate(condition_id="0xb", question="B?"),
    ]

    merged = write_tracked_markets(candidates, str(path))

    assert len(merged) == 2
    by_id = {entry["condition_id"]: entry for entry in merged}
    assert by_id["0xa"]["question"] == "A?"  # unchanged, not overwritten
    assert by_id["0xb"]["question"] == "B?"


def test_write_tracked_markets_preserves_entries_not_in_the_new_candidates(tmp_path):
    path = tmp_path / "tracked_markets.json"
    path.write_text(json.dumps([{"condition_id": "0xold", "question": "Old market"}]))

    merged = write_tracked_markets([make_candidate(condition_id="0xnew", question="New?")], str(path))

    assert {entry["condition_id"] for entry in merged} == {"0xold", "0xnew"}


def test_write_tracked_markets_recovers_from_malformed_existing_file(tmp_path):
    path = tmp_path / "tracked_markets.json"
    path.write_text("not json")

    merged = write_tracked_markets([make_candidate(condition_id="0xa", question="A?")], str(path))

    assert merged == [{"condition_id": "0xa", "question": "A?"}]
