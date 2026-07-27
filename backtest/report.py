"""Assembles BacktestResult from a finished replay, renders a text summary,
and (optionally) plots the equity curve. matplotlib is imported lazily inside
plot_equity_curve() so computing metrics/reports never requires it.
"""

from dataclasses import dataclass, field
from datetime import datetime

from backtest.metrics import brier_score, log_loss, max_drawdown, sharpe_ratio
from backtest.portfolio import Fill, Portfolio


@dataclass(frozen=True)
class Prediction:
    """A probability a strategy implied for a token at the moment it traded,
    recorded so it can be graded against the market's eventual resolution."""

    token_id: str
    probability: float


@dataclass(frozen=True)
class BacktestResult:
    strategy_names: list[str]
    initial_cash: float
    final_equity: float
    realized_pnl: float
    total_pnl: float
    num_fills: int
    fills: list[Fill] = field(repr=False)
    equity_curve: list[tuple[datetime, float]] = field(repr=False)
    sharpe: float = 0.0
    max_drawdown: float = 0.0
    brier: float | None = None
    log_loss: float | None = None
    num_graded_predictions: int = 0

    @property
    def unrealized_pnl(self) -> float:
        return self.total_pnl - self.realized_pnl

    @property
    def total_return_pct(self) -> float:
        if self.initial_cash == 0:
            return 0.0
        return self.total_pnl / self.initial_cash


def build_result(
    strategy_names: list[str],
    portfolio: Portfolio,
    equity_curve: list[tuple[datetime, float]],
    graded_predictions: list[tuple[float, float]],
) -> BacktestResult:
    final_equity = equity_curve[-1][1] if equity_curve else portfolio.cash
    equity_values = [value for _, value in equity_curve]

    brier = ll = None
    if graded_predictions:
        probabilities = [p for p, _ in graded_predictions]
        outcomes = [o for _, o in graded_predictions]
        brier = brier_score(probabilities, outcomes)
        ll = log_loss(probabilities, outcomes)

    return BacktestResult(
        strategy_names=strategy_names,
        initial_cash=portfolio.initial_cash,
        final_equity=final_equity,
        realized_pnl=portfolio.realized_pnl,
        total_pnl=final_equity - portfolio.initial_cash,
        num_fills=len(portfolio.fills),
        fills=list(portfolio.fills),
        equity_curve=equity_curve,
        sharpe=sharpe_ratio(_period_returns(equity_values)),
        max_drawdown=max_drawdown(equity_values),
        brier=brier,
        log_loss=ll,
        num_graded_predictions=len(graded_predictions),
    )


def _period_returns(equity_values: list[float]) -> list[float]:
    returns = []
    for previous, current in zip(equity_values, equity_values[1:]):
        if previous != 0:
            returns.append((current - previous) / previous)
    return returns


def generate_report(result: BacktestResult) -> str:
    lines = [
        f"Backtest report: {', '.join(result.strategy_names)}",
        "=" * 48,
        f"Initial cash:        ${result.initial_cash:,.2f}",
        f"Final equity:        ${result.final_equity:,.2f}",
        f"Total P&L:           ${result.total_pnl:,.2f} ({result.total_return_pct:+.2%})",
        f"  Realized:          ${result.realized_pnl:,.2f}",
        f"  Unrealized:        ${result.unrealized_pnl:,.2f}",
        f"Fills:               {result.num_fills}",
        f"Sharpe (per-tick):   {result.sharpe:.3f}",
        f"Max drawdown:        {result.max_drawdown:.2%}",
    ]
    if result.brier is not None:
        lines.append(
            f"Brier score:         {result.brier:.4f} "
            f"({result.num_graded_predictions} graded predictions)"
        )
        lines.append(f"Log loss:            {result.log_loss:.4f}")
    else:
        lines.append("Brier score / log loss: n/a (no resolved-market predictions to grade)")
    return "\n".join(lines)


def plot_equity_curve(result: BacktestResult, output_path: str) -> None:
    """Save a simple equity-curve line plot to output_path (e.g. a .png)."""
    if not result.equity_curve:
        raise ValueError("no equity curve data to plot")

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    timestamps = [t for t, _ in result.equity_curve]
    values = [v for _, v in result.equity_curve]

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(timestamps, values)
    ax.set_title(f"Equity curve: {', '.join(result.strategy_names)}")
    ax.set_xlabel("Time")
    ax.set_ylabel("Equity ($)")
    ax.grid(True, alpha=0.3)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(output_path)
    plt.close(fig)
