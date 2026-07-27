"""Fractional Kelly position sizing for a binary (YES/NO) prediction-market
bet. Pure, stateless - kept separate from OrderManager so it's independently
testable, the same way signals/market_making/spread.py is kept separate from
MarketMakingStrategy.

For a share priced at `current_price` (a probability in (0, 1)) with implied
fair value `fair_price`, the full-Kelly stake as a fraction of bankroll is:

  BUY:  f* = (fair_price - current_price) / (1 - current_price)
  SELL: f* = (current_price - fair_price) / current_price

(Standard Kelly derivation for a bet that risks `current_price` to win
`1 - current_price` with probability `fair_price`, or the mirror image for
selling.) The full-Kelly fraction is then scaled down by the signal's
confidence and a configured kelly_fraction (e.g. 0.25 for quarter-Kelly)
before being converted to a dollar amount.
"""

from dataclasses import dataclass

from signals.base import Side


@dataclass(frozen=True)
class KellyParams:
    kelly_fraction: float = 0.25
    """Fraction of full Kelly actually staked (e.g. 0.25 = quarter-Kelly)."""

    max_stake_fraction: float = 1.0
    """Hard clamp on the final (post-fraction, post-confidence) fraction of
    bankroll to risk on a single position."""


def implied_fair_price(current_price: float, edge_estimate: float, side: Side) -> float:
    """Back out the strategy's implied fair-value probability from its edge
    estimate: for a BUY signal, edge_estimate is how far above current_price
    fair value sits; for SELL, how far below. Clamped to a valid probability."""
    fair_price = (
        current_price + edge_estimate if side is Side.BUY else current_price - edge_estimate
    )
    return max(0.0, min(1.0, fair_price))


def kelly_stake_fraction(
    current_price: float,
    fair_price: float,
    side: Side,
    confidence: float,
    params: KellyParams = KellyParams(),
) -> float:
    """Full-Kelly fraction of bankroll for this side, scaled by confidence and
    kelly_fraction. Returns 0 if there's no edge in the requested direction,
    or the price sits at a boundary (0 or 1) where the formula is undefined
    for that side."""
    if side is Side.BUY:
        if current_price >= 1.0 or fair_price <= current_price:
            return 0.0
        full_kelly = (fair_price - current_price) / (1.0 - current_price)
    else:
        if current_price <= 0.0 or fair_price >= current_price:
            return 0.0
        full_kelly = (current_price - fair_price) / current_price

    sized = full_kelly * confidence * params.kelly_fraction
    return max(0.0, min(sized, params.max_stake_fraction))


def kelly_position_size_usd(
    current_price: float,
    edge_estimate: float,
    side: Side,
    confidence: float,
    bankroll_usd: float,
    params: KellyParams = KellyParams(),
) -> float:
    """The full pipeline: edge_estimate -> implied fair price -> Kelly
    fraction -> dollar notional to risk. 0 for degenerate inputs (price at a
    boundary, non-positive bankroll) or when the fraction works out to 0."""
    if current_price <= 0.0 or current_price >= 1.0 or bankroll_usd <= 0.0:
        return 0.0
    fair_price = implied_fair_price(current_price, edge_estimate, side)
    fraction = kelly_stake_fraction(current_price, fair_price, side, confidence, params)
    return fraction * bankroll_usd
