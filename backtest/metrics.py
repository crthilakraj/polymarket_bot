"""Pure backtest performance metrics - kept separate from the engine/portfolio
so each is independently testable against known expected values, mirroring
signals/market_making/spread.py and execution/sizing.py's isolation pattern.
"""

import math


def brier_score(predicted_probabilities: list[float], outcomes: list[float]) -> float:
    """Mean squared error between predicted probabilities and binary outcomes
    (0.0 or 1.0). Lower is better: 0 is perfect, 0.25 is what a coinflip
    forecaster scores against a 50/50 base rate."""
    if len(predicted_probabilities) != len(outcomes):
        raise ValueError("predicted_probabilities and outcomes must be the same length")
    if not predicted_probabilities:
        return 0.0
    return sum((p - o) ** 2 for p, o in zip(predicted_probabilities, outcomes)) / len(outcomes)


def log_loss(predicted_probabilities: list[float], outcomes: list[float], eps: float = 1e-15) -> float:
    """Mean negative log-likelihood of the outcomes under the predicted
    probabilities. Probabilities are clipped away from 0/1 by eps so a
    maximally-wrong confident prediction costs a large but finite amount
    rather than -inf. Lower is better."""
    if len(predicted_probabilities) != len(outcomes):
        raise ValueError("predicted_probabilities and outcomes must be the same length")
    if not predicted_probabilities:
        return 0.0
    total = 0.0
    for p, o in zip(predicted_probabilities, outcomes):
        clipped = min(max(p, eps), 1 - eps)
        total += -(o * math.log(clipped) + (1 - o) * math.log(1 - clipped))
    return total / len(outcomes)


def sharpe_ratio(
    returns: list[float], risk_free_rate: float = 0.0, periods_per_year: float | None = None
) -> float:
    """Mean excess return over its sample stdev, i.e. Sharpe over whatever
    period `returns` is sampled at. Pass periods_per_year to annualize
    (multiplies by sqrt(periods_per_year)). 0.0 for fewer than 2 returns or
    zero variance (a flat or single-point series has no ratio to report)."""
    if len(returns) < 2:
        return 0.0
    excess = [r - risk_free_rate for r in returns]
    mean = sum(excess) / len(excess)
    variance = sum((r - mean) ** 2 for r in excess) / (len(excess) - 1)
    stdev = math.sqrt(variance)
    if stdev == 0:
        return 0.0
    ratio = mean / stdev
    if periods_per_year:
        ratio *= math.sqrt(periods_per_year)
    return ratio


def max_drawdown(equity_curve: list[float]) -> float:
    """Largest peak-to-trough decline as a fraction of the peak (e.g. 0.2 ==
    a 20% drawdown from the running high). 0.0 for an empty, single-point, or
    never-declining curve."""
    if len(equity_curve) < 2:
        return 0.0
    peak = equity_curve[0]
    worst = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return worst
