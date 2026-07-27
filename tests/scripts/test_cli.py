from datetime import datetime, timezone

from execution.journal import DecisionJournal
from execution.models import OrderDecision, OrderIntent, OrderStatus
from scripts.cli import _replay_portfolio
from signals.base import Side

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_intent(**overrides) -> OrderIntent:
    defaults = dict(
        token_id="t1",
        condition_id="0xcond",
        side=Side.BUY,
        price=0.40,
        size=100.0,
        strategy="signal",
        idempotency_key="key1",
    )
    defaults.update(overrides)
    return OrderIntent(**defaults)


def test_replay_portfolio_applies_dry_run_and_submitted_fills(tmp_path):
    journal = DecisionJournal(tmp_path / "journal.db")
    journal.record_decision(
        strategy="signal",
        condition_id="0xcond",
        decision=OrderDecision(status=OrderStatus.DRY_RUN, intent=make_intent()),
        timestamp=NOW,
    )
    journal.record_decision(
        strategy="signal",
        condition_id="0xcond",
        decision=OrderDecision(
            status=OrderStatus.SUBMITTED, intent=make_intent(price=0.42, size=10.0), order_id="o1"
        ),
        timestamp=NOW,
    )

    portfolio = _replay_portfolio(journal, initial_cash=1000.0)

    assert portfolio.positions["t1"].shares == 110.0
    journal.close()


def test_replay_portfolio_skips_rejected_and_failed_decisions(tmp_path):
    journal = DecisionJournal(tmp_path / "journal.db")
    journal.record_decision(
        strategy="signal",
        condition_id="0xcond",
        decision=OrderDecision(status=OrderStatus.REJECTED, intent=make_intent(), reasons=["no room"]),
        timestamp=NOW,
    )
    journal.record_decision(
        strategy="signal",
        condition_id="0xcond",
        decision=OrderDecision(status=OrderStatus.FAILED, intent=make_intent()),
        timestamp=NOW,
    )

    portfolio = _replay_portfolio(journal, initial_cash=1000.0)

    assert portfolio.positions == {}
    assert portfolio.cash == 1000.0
    journal.close()


def test_replay_portfolio_skips_decisions_with_no_intent(tmp_path):
    journal = DecisionJournal(tmp_path / "journal.db")
    journal.record_decision(
        strategy="signal",
        condition_id="0xcond",
        decision=OrderDecision(status=OrderStatus.REJECTED, intent=None, reasons=["no token_id"]),
        timestamp=NOW,
    )

    portfolio = _replay_portfolio(journal, initial_cash=1000.0)

    assert portfolio.positions == {}
    journal.close()


def test_replay_portfolio_tracks_realized_pnl_across_multiple_fills(tmp_path):
    journal = DecisionJournal(tmp_path / "journal.db")
    journal.record_decision(
        strategy="signal",
        condition_id="0xcond",
        decision=OrderDecision(status=OrderStatus.DRY_RUN, intent=make_intent(side=Side.BUY, price=0.40, size=100)),
        timestamp=NOW,
    )
    journal.record_decision(
        strategy="signal",
        condition_id="0xcond",
        decision=OrderDecision(status=OrderStatus.DRY_RUN, intent=make_intent(side=Side.SELL, price=0.55, size=40)),
        timestamp=NOW,
    )

    portfolio = _replay_portfolio(journal, initial_cash=1000.0)

    assert portfolio.realized_pnl == (0.55 - 0.40) * 40
    assert portfolio.positions["t1"].shares == 60
    journal.close()
