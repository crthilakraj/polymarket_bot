from data.ingest import _resolve_token_ids
from data.models import MarketMetadata


def test_resolve_token_ids_maps_each_outcome_to_its_condition():
    market = MarketMetadata(
        condition_id="0xcond",
        question_id=None,
        question=None,
        description=None,
        resolution_source=None,
        category=None,
        end_date=None,
        active=None,
        closed=None,
        outcomes=["Yes", "No"],
        outcome_prices=[0.5, 0.5],
        token_ids=["111", "222"],
    )

    mapping = _resolve_token_ids([market])

    assert mapping == {"111": "0xcond", "222": "0xcond"}
