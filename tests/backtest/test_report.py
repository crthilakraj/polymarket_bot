from datetime import datetime, timezone

import pytest

from backtest.portfolio import Portfolio
from backtest.report import build_result, generate_report, plot_equity_curve
from signals.base import Side

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_portfolio_with_a_winning_trade() -> Portfolio:
    portfolio = Portfolio(initial_cash=1000.0)
    portfolio.apply_fill("t1", "0xcond", Side.BUY, price=0.40, size=100, timestamp=NOW, strategy="s")
    portfolio.settle({"t1": 1.0})
    return portfolio


def test_build_result_computes_total_and_realized_pnl():
    portfolio = make_portfolio_with_a_winning_trade()
    equity_curve = [(NOW, 1000.0), (NOW, portfolio.mark_to_market({}))]

    result = build_result(["s"], portfolio, equity_curve, graded_predictions=[])

    assert result.realized_pnl == pytest.approx((1.0 - 0.40) * 100)
    assert result.total_pnl == pytest.approx(result.final_equity - result.initial_cash)
    assert result.unrealized_pnl == pytest.approx(result.total_pnl - result.realized_pnl)


def test_build_result_grades_predictions_when_present():
    portfolio = Portfolio(initial_cash=1000.0)
    equity_curve = [(NOW, 1000.0)]

    result = build_result(["s"], portfolio, equity_curve, graded_predictions=[(0.9, 1.0), (0.1, 0.0)])

    assert result.brier is not None
    assert result.log_loss is not None
    assert result.num_graded_predictions == 2


def test_build_result_leaves_calibration_metrics_none_without_predictions():
    portfolio = Portfolio(initial_cash=1000.0)
    result = build_result(["s"], portfolio, [(NOW, 1000.0)], graded_predictions=[])

    assert result.brier is None
    assert result.log_loss is None
    assert result.num_graded_predictions == 0


def test_build_result_handles_empty_equity_curve():
    portfolio = Portfolio(initial_cash=1000.0)
    result = build_result(["s"], portfolio, [], graded_predictions=[])

    assert result.final_equity == portfolio.cash
    assert result.sharpe == 0.0
    assert result.max_drawdown == 0.0


def test_generate_report_includes_key_figures():
    portfolio = make_portfolio_with_a_winning_trade()
    equity_curve = [(NOW, 1000.0), (NOW, portfolio.mark_to_market({}))]
    result = build_result(["arb"], portfolio, equity_curve, graded_predictions=[(0.9, 1.0)])

    text = generate_report(result)

    assert "arb" in text
    assert "Total P&L" in text
    assert "Brier score" in text
    assert "Sharpe" in text
    assert "Max drawdown" in text


def test_generate_report_notes_missing_calibration_data():
    portfolio = Portfolio(initial_cash=1000.0)
    result = build_result(["s"], portfolio, [(NOW, 1000.0)], graded_predictions=[])

    text = generate_report(result)

    assert "n/a" in text


def test_plot_equity_curve_writes_a_file(tmp_path):
    portfolio = make_portfolio_with_a_winning_trade()
    equity_curve = [(NOW, 1000.0), (NOW, portfolio.mark_to_market({}))]
    result = build_result(["arb"], portfolio, equity_curve, graded_predictions=[])

    output_path = tmp_path / "equity.png"
    plot_equity_curve(result, str(output_path))

    assert output_path.exists()
    assert output_path.stat().st_size > 0


def test_plot_equity_curve_raises_without_data(tmp_path):
    portfolio = Portfolio(initial_cash=1000.0)
    result = build_result(["s"], portfolio, [], graded_predictions=[])

    with pytest.raises(ValueError):
        plot_equity_curve(result, str(tmp_path / "should-not-be-created.png"))
