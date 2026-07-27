import pytest

from signals.market_making.spread import (
    SpreadParams,
    compute_half_spread_bps,
    inventory_skew_factor,
    time_to_resolution_factor,
    volatility_factor,
)


# --- time_to_resolution_factor -------------------------------------------------


def test_time_factor_is_neutral_when_far_from_resolution():
    params = SpreadParams(time_widen_horizon_hours=24.0, time_widen_max_multiplier=4.0)

    assert time_to_resolution_factor(48.0, params) == 1.0
    assert time_to_resolution_factor(24.0, params) == 1.0  # exactly at the horizon


def test_time_factor_is_neutral_when_resolution_unknown():
    params = SpreadParams()

    assert time_to_resolution_factor(None, params) == 1.0


def test_time_factor_ramps_linearly_toward_resolution():
    params = SpreadParams(time_widen_horizon_hours=24.0, time_widen_max_multiplier=4.0)

    # Halfway through the horizon -> halfway between 1.0 and 4.0
    assert time_to_resolution_factor(12.0, params) == pytest.approx(2.5)
    # Three quarters through -> three quarters of the way to 4.0
    assert time_to_resolution_factor(6.0, params) == pytest.approx(3.25)


def test_time_factor_hits_max_at_and_past_resolution():
    params = SpreadParams(time_widen_max_multiplier=4.0)

    assert time_to_resolution_factor(0.0, params) == 4.0
    assert time_to_resolution_factor(-5.0, params) == 4.0  # already past resolution


def test_time_factor_is_monotonically_increasing_as_time_shrinks():
    params = SpreadParams(time_widen_horizon_hours=24.0)
    hours = [24.0, 18.0, 12.0, 6.0, 1.0, 0.0]
    factors = [time_to_resolution_factor(h, params) for h in hours]

    assert factors == sorted(factors)


# --- inventory_skew_factor ------------------------------------------------------


def test_inventory_factor_is_neutral_at_zero_skew():
    assert inventory_skew_factor(0.0, SpreadParams()) == 1.0


def test_inventory_factor_hits_max_at_full_skew_either_direction():
    params = SpreadParams(inventory_widen_max_multiplier=3.0)

    assert inventory_skew_factor(1.0, params) == 3.0
    assert inventory_skew_factor(-1.0, params) == 3.0


def test_inventory_factor_is_symmetric_around_zero():
    params = SpreadParams()

    assert inventory_skew_factor(0.4, params) == inventory_skew_factor(-0.4, params)


def test_inventory_factor_scales_linearly_with_skew_magnitude():
    params = SpreadParams(inventory_widen_max_multiplier=3.0)

    assert inventory_skew_factor(0.5, params) == pytest.approx(2.0)  # halfway to max


def test_inventory_factor_clamps_skew_beyond_unit_range():
    params = SpreadParams(inventory_widen_max_multiplier=3.0)

    assert inventory_skew_factor(5.0, params) == 3.0
    assert inventory_skew_factor(-5.0, params) == 3.0


# --- volatility_factor -----------------------------------------------------------


def test_volatility_factor_is_neutral_at_zero_volatility():
    assert volatility_factor(0.0, SpreadParams()) == 1.0


def test_volatility_factor_grows_linearly_with_volatility():
    params = SpreadParams(volatility_widen_sensitivity=8.0)

    assert volatility_factor(0.05, params) == pytest.approx(1.4)
    assert volatility_factor(0.10, params) == pytest.approx(1.8)


def test_volatility_factor_clamps_negative_volatility_to_neutral():
    assert volatility_factor(-1.0, SpreadParams()) == 1.0


# --- compute_half_spread_bps: combinator -----------------------------------------


def test_compute_half_spread_is_base_at_neutral_inputs():
    params = SpreadParams(base_half_spread_bps=50.0)

    result = compute_half_spread_bps(
        hours_remaining=48.0, inventory_skew=0.0, normalized_volatility=0.0, params=params
    )

    assert result == pytest.approx(50.0)


def test_compute_half_spread_combines_factors_multiplicatively():
    params = SpreadParams(
        base_half_spread_bps=50.0,
        time_widen_horizon_hours=24.0,
        time_widen_max_multiplier=4.0,
        inventory_widen_max_multiplier=3.0,
        volatility_widen_sensitivity=8.0,
        max_half_spread_bps=100_000.0,  # effectively no ceiling for this test
    )

    result = compute_half_spread_bps(
        hours_remaining=0.0,  # time factor: 4.0
        inventory_skew=1.0,  # inventory factor: 3.0
        normalized_volatility=0.125,  # volatility factor: 1 + 0.125*8 = 2.0
        params=params,
    )

    assert result == pytest.approx(50.0 * 4.0 * 3.0 * 2.0)


def test_compute_half_spread_is_clipped_to_max():
    params = SpreadParams(
        base_half_spread_bps=50.0,
        time_widen_max_multiplier=4.0,
        inventory_widen_max_multiplier=3.0,
        volatility_widen_sensitivity=8.0,
        max_half_spread_bps=200.0,
    )

    result = compute_half_spread_bps(
        hours_remaining=0.0, inventory_skew=1.0, normalized_volatility=1.0, params=params
    )

    assert result == 200.0


@pytest.mark.parametrize(
    "hours_remaining,inventory_skew,normalized_volatility",
    [
        (48.0, 0.0, 0.0),
        (12.0, 0.0, 0.0),
        (0.0, 0.0, 0.0),
        (48.0, 0.5, 0.0),
        (48.0, 1.0, 0.0),
        (48.0, 0.0, 0.05),
        (48.0, 0.0, 0.2),
    ],
)
def test_compute_half_spread_never_falls_below_base(
    hours_remaining, inventory_skew, normalized_volatility
):
    params = SpreadParams(base_half_spread_bps=50.0)

    result = compute_half_spread_bps(hours_remaining, inventory_skew, normalized_volatility, params)

    assert result >= params.base_half_spread_bps
