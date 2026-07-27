from datetime import datetime, timezone

from execution.journal import DecisionJournal
from execution.models import OrderDecision, OrderIntent, OrderStatus
from signals.base import Side, Signal
from signals.market_making.models import Quote, QuotePair

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_signal(**overrides) -> Signal:
    defaults = dict(edge_estimate=0.10, confidence=0.8, side=Side.BUY, token_id="t1")
    defaults.update(overrides)
    return Signal(**defaults)


def make_decision(**overrides) -> OrderDecision:
    intent = OrderIntent(
        token_id="t1",
        condition_id="0xcond",
        side=Side.BUY,
        price=0.45,
        size=10.0,
        strategy="signal",
        idempotency_key="abc123",
    )
    defaults = dict(status=OrderStatus.DRY_RUN, intent=intent, reasons=[], order_id=None)
    defaults.update(overrides)
    return OrderDecision(**defaults)


def test_record_and_read_back_a_signal(tmp_path):
    journal = DecisionJournal(tmp_path / "journal.db")
    journal.record_signal(strategy="news", condition_id="0xcond", signal=make_signal(), timestamp=NOW)

    records = journal.recent_activity()

    assert len(records) == 1
    record = records[0]
    assert record.kind == "signal"
    assert record.strategy == "news"
    assert record.token_id == "t1"
    assert record.side == "BUY"
    assert record.edge_estimate == 0.10
    assert record.confidence == 0.8
    journal.close()


def test_record_and_read_back_a_quote(tmp_path):
    journal = DecisionJournal(tmp_path / "journal.db")
    quote_pair = QuotePair(token_id="t1", bid=Quote(0.44, 10.0), ask=Quote(0.46, 5.0))

    journal.record_quote(strategy="mm", condition_id="0xcond", quote_pair=quote_pair, timestamp=NOW)

    records = journal.recent_activity()
    assert len(records) == 1
    record = records[0]
    assert record.kind == "quote"
    assert record.bid_price == 0.44
    assert record.bid_size == 10.0
    assert record.ask_price == 0.46
    assert record.ask_size == 5.0
    journal.close()


def test_record_quote_with_missing_side_stores_nulls(tmp_path):
    journal = DecisionJournal(tmp_path / "journal.db")
    quote_pair = QuotePair(token_id="t1", bid=Quote(0.44, 10.0), ask=None)

    journal.record_quote(strategy="mm", condition_id="0xcond", quote_pair=quote_pair, timestamp=NOW)

    record = journal.recent_activity()[0]
    assert record.ask_price is None
    assert record.ask_size is None
    journal.close()


def test_record_and_read_back_a_decision(tmp_path):
    journal = DecisionJournal(tmp_path / "journal.db")
    decision = make_decision(reasons=["resized to max_order_usd ($25.00)"], order_id="order-1")

    journal.record_decision(strategy="signal", condition_id="0xcond", decision=decision, timestamp=NOW)

    records = journal.recent_decisions()
    assert len(records) == 1
    record = records[0]
    assert record.token_id == "t1"
    assert record.side == "BUY"
    assert record.price == 0.45
    assert record.size == 10.0
    assert record.status == "DRY_RUN"
    assert record.reasons == ["resized to max_order_usd ($25.00)"]
    assert record.order_id == "order-1"
    journal.close()


def test_record_decision_with_no_intent_stores_nulls(tmp_path):
    journal = DecisionJournal(tmp_path / "journal.db")
    decision = OrderDecision(status=OrderStatus.REJECTED, intent=None, reasons=["no room"])

    journal.record_decision(strategy="signal", condition_id="0xcond", decision=decision, timestamp=NOW)

    record = journal.recent_decisions()[0]
    assert record.token_id is None
    assert record.side is None
    assert record.status == "REJECTED"
    assert record.reasons == ["no room"]
    journal.close()


def test_recent_activity_respects_limit_and_orders_newest_first(tmp_path):
    journal = DecisionJournal(tmp_path / "journal.db")
    for day in (1, 2, 3):
        journal.record_signal(
            strategy="news",
            condition_id="0xcond",
            signal=make_signal(),
            timestamp=datetime(2026, 1, day, tzinfo=timezone.utc),
        )

    records = journal.recent_activity(limit=2)

    assert len(records) == 2
    assert records[0].timestamp.day == 3
    assert records[1].timestamp.day == 2
    journal.close()


def test_all_decisions_returns_oldest_first(tmp_path):
    journal = DecisionJournal(tmp_path / "journal.db")
    for day in (3, 1, 2):
        journal.record_decision(
            strategy="signal",
            condition_id="0xcond",
            decision=make_decision(),
            timestamp=datetime(2026, 1, day, tzinfo=timezone.utc),
        )

    records = journal.all_decisions()

    assert [r.timestamp.day for r in records] == [1, 2, 3]
    journal.close()
