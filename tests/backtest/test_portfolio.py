from datetime import datetime, timezone

import pytest

from backtest.portfolio import Portfolio
from signals.base import Side

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_buy_fill_reduces_cash_and_opens_a_long_position():
    portfolio = Portfolio(initial_cash=1000.0)

    portfolio.apply_fill("t1", "0xcond", Side.BUY, price=0.40, size=100, timestamp=NOW, strategy="s")

    assert portfolio.cash == pytest.approx(1000.0 - 40.0)
    assert portfolio.positions["t1"].shares == 100
    assert portfolio.positions["t1"].avg_cost == pytest.approx(0.40)


def test_sell_fill_with_no_position_opens_a_short():
    portfolio = Portfolio(initial_cash=1000.0)

    portfolio.apply_fill("t1", "0xcond", Side.SELL, price=0.40, size=50, timestamp=NOW, strategy="s")

    assert portfolio.cash == pytest.approx(1000.0 + 20.0)
    assert portfolio.positions["t1"].shares == -50


def test_extending_a_long_position_updates_weighted_average_cost():
    portfolio = Portfolio(initial_cash=1000.0)
    portfolio.apply_fill("t1", "0xcond", Side.BUY, price=0.40, size=100, timestamp=NOW, strategy="s")
    portfolio.apply_fill("t1", "0xcond", Side.BUY, price=0.60, size=100, timestamp=NOW, strategy="s")

    position = portfolio.positions["t1"]
    assert position.shares == 200
    assert position.avg_cost == pytest.approx(0.50)  # (0.40*100 + 0.60*100) / 200


def test_partial_close_realizes_pnl_on_the_closed_portion_only():
    portfolio = Portfolio(initial_cash=1000.0)
    portfolio.apply_fill("t1", "0xcond", Side.BUY, price=0.40, size=100, timestamp=NOW, strategy="s")

    portfolio.apply_fill("t1", "0xcond", Side.SELL, price=0.70, size=40, timestamp=NOW, strategy="s")

    assert portfolio.realized_pnl == pytest.approx((0.70 - 0.40) * 40)
    assert portfolio.positions["t1"].shares == 60
    assert portfolio.positions["t1"].avg_cost == pytest.approx(0.40)  # unchanged for the remainder


def test_closing_a_position_exactly_zeroes_avg_cost():
    portfolio = Portfolio(initial_cash=1000.0)
    portfolio.apply_fill("t1", "0xcond", Side.BUY, price=0.40, size=100, timestamp=NOW, strategy="s")

    portfolio.apply_fill("t1", "0xcond", Side.SELL, price=0.55, size=100, timestamp=NOW, strategy="s")

    position = portfolio.positions["t1"]
    assert position.shares == 0
    assert position.avg_cost == 0.0
    assert portfolio.realized_pnl == pytest.approx((0.55 - 0.40) * 100)


def test_flipping_through_zero_opens_a_fresh_position_at_the_flip_price():
    portfolio = Portfolio(initial_cash=1000.0)
    portfolio.apply_fill("t1", "0xcond", Side.BUY, price=0.40, size=100, timestamp=NOW, strategy="s")

    portfolio.apply_fill("t1", "0xcond", Side.SELL, price=0.50, size=150, timestamp=NOW, strategy="s")

    position = portfolio.positions["t1"]
    assert position.shares == -50
    assert position.avg_cost == pytest.approx(0.50)
    assert portfolio.realized_pnl == pytest.approx((0.50 - 0.40) * 100)  # only the closed 100 realized


def test_short_position_realizes_pnl_symmetrically():
    portfolio = Portfolio(initial_cash=1000.0)
    portfolio.apply_fill("t1", "0xcond", Side.SELL, price=0.60, size=100, timestamp=NOW, strategy="s")

    portfolio.apply_fill("t1", "0xcond", Side.BUY, price=0.40, size=100, timestamp=NOW, strategy="s")

    # Shorted at 0.60, covered at 0.40 -> profit of 0.20/share.
    assert portfolio.realized_pnl == pytest.approx((0.60 - 0.40) * 100)
    assert portfolio.positions["t1"].shares == 0


def test_apply_fill_rejects_non_positive_size():
    portfolio = Portfolio(initial_cash=1000.0)
    with pytest.raises(ValueError):
        portfolio.apply_fill("t1", "0xcond", Side.BUY, price=0.5, size=0, timestamp=NOW, strategy="s")
    with pytest.raises(ValueError):
        portfolio.apply_fill("t1", "0xcond", Side.BUY, price=0.5, size=-1, timestamp=NOW, strategy="s")


def test_mark_to_market_includes_cash_and_open_positions():
    portfolio = Portfolio(initial_cash=1000.0)
    portfolio.apply_fill("t1", "0xcond", Side.BUY, price=0.40, size=100, timestamp=NOW, strategy="s")

    equity = portfolio.mark_to_market({"t1": 0.55})

    assert equity == pytest.approx((1000.0 - 40.0) + 100 * 0.55)


def test_mark_to_market_falls_back_to_avg_cost_when_price_unknown():
    portfolio = Portfolio(initial_cash=1000.0)
    portfolio.apply_fill("t1", "0xcond", Side.BUY, price=0.40, size=100, timestamp=NOW, strategy="s")

    equity = portfolio.mark_to_market({})

    assert equity == pytest.approx(1000.0)  # cash - 40 + 100*0.40 == 1000


def test_settle_converts_resolved_positions_to_cash_and_realized_pnl():
    portfolio = Portfolio(initial_cash=1000.0)
    portfolio.apply_fill("t1", "0xcond", Side.BUY, price=0.40, size=100, timestamp=NOW, strategy="s")

    portfolio.settle({"t1": 1.0})  # resolved YES

    assert portfolio.positions["t1"].shares == 0.0
    assert portfolio.realized_pnl == pytest.approx((1.0 - 0.40) * 100)
    assert portfolio.cash == pytest.approx(1000.0 - 40.0 + 100.0)


def test_settle_ignores_tokens_not_in_resolutions():
    portfolio = Portfolio(initial_cash=1000.0)
    portfolio.apply_fill("t1", "0xcond", Side.BUY, price=0.40, size=100, timestamp=NOW, strategy="s")

    portfolio.settle({"t-other": 1.0})

    assert portfolio.positions["t1"].shares == 100  # left open
