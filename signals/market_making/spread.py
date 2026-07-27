"""Pure, stateless spread-widening logic for market making - kept separate from
the stateful MarketMakingStrategy so it can be tested in isolation, with no
market/order-book/inventory objects involved.

The quoted half-spread is a base half-spread scaled up by three independent
widening factors, each >= 1.0, combined multiplicatively and clipped to a
hard ceiling:

  - time factor:        widens as time-to-resolution shrinks
  - inventory factor:   widens as position skews away from flat
  - volatility factor:  widens as recent order book volatility increases
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class SpreadParams:
    base_half_spread_bps: float = 50.0
    """Half-spread (basis points of mid price) at zero skew/volatility, far from resolution."""

    max_half_spread_bps: float = 500.0
    """Hard ceiling on the half-spread regardless of how wide the factors push it."""

    time_widen_horizon_hours: float = 24.0
    """Widening ramps up linearly over this many hours before resolution."""

    time_widen_max_multiplier: float = 4.0
    """Spread multiplier reached right at (or past) resolution."""

    inventory_widen_max_multiplier: float = 3.0
    """Spread multiplier reached at full inventory skew (|skew| == 1)."""

    volatility_widen_sensitivity: float = 8.0
    """Multiplier added per unit of normalized volatility (e.g. 0.05 -> +40% at sensitivity 8)."""


def time_to_resolution_factor(hours_remaining: float | None, params: SpreadParams) -> float:
    """1.0 while hours_remaining is at or beyond the widening horizon, ramping
    linearly up to time_widen_max_multiplier as it shrinks to zero. Unknown
    resolution time (None) is treated as neutral - no widening."""
    if hours_remaining is None:
        return 1.0
    if hours_remaining <= 0:
        return params.time_widen_max_multiplier
    if hours_remaining >= params.time_widen_horizon_hours:
        return 1.0
    fraction_elapsed = 1.0 - (hours_remaining / params.time_widen_horizon_hours)
    return 1.0 + fraction_elapsed * (params.time_widen_max_multiplier - 1.0)


def inventory_skew_factor(inventory_skew: float, params: SpreadParams) -> float:
    """1.0 at zero skew, ramping linearly up to inventory_widen_max_multiplier at
    |inventory_skew| == 1. inventory_skew is expected in [-1, 1] (position as a
    fraction of the max position cap) but is clamped defensively."""
    magnitude = min(abs(inventory_skew), 1.0)
    return 1.0 + magnitude * (params.inventory_widen_max_multiplier - 1.0)


def volatility_factor(normalized_volatility: float, params: SpreadParams) -> float:
    """1.0 at zero volatility, growing linearly with normalized_volatility (e.g.
    the stdev of recent mid-price returns). Never below 1.0 even if a caller
    passes a negative value."""
    magnitude = max(normalized_volatility, 0.0)
    return 1.0 + magnitude * params.volatility_widen_sensitivity


def compute_half_spread_bps(
    hours_remaining: float | None,
    inventory_skew: float,
    normalized_volatility: float,
    params: SpreadParams = SpreadParams(),
) -> float:
    """Combine the three widening factors multiplicatively onto the base
    half-spread, clipped to max_half_spread_bps."""
    multiplier = (
        time_to_resolution_factor(hours_remaining, params)
        * inventory_skew_factor(inventory_skew, params)
        * volatility_factor(normalized_volatility, params)
    )
    return min(params.base_half_spread_bps * multiplier, params.max_half_spread_bps)
