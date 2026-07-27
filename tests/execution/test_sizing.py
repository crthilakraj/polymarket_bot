import pytest

from execution.sizing import (
    KellyParams,
    implied_fair_price,
    kelly_position_size_usd,
    kelly_stake_fraction,
)
from signals.base import Side


# --- implied_fair_price ---------------------------------------------------------


def test_implied_fair_price_buy_adds_edge():
    assert implied_fair_price(0.40, 0.10, Side.BUY) == pytest.approx(0.50)


def test_implied_fair_price_sell_subtracts_edge():
    assert implied_fair_price(0.60, 0.10, Side.SELL) == pytest.approx(0.50)


def test_implied_fair_price_clamps_to_valid_probability():
    assert implied_fair_price(0.95, 0.50, Side.BUY) == 1.0
    assert implied_fair_price(0.05, 0.50, Side.SELL) == 0.0


# --- kelly_stake_fraction --------------------------------------------------------


def test_kelly_fraction_matches_closed_form_for_buy():
    # f* = (fair - price) / (1 - price) = (0.60 - 0.40) / (1 - 0.40) = 1/3
    params = KellyParams(kelly_fraction=1.0)
    fraction = kelly_stake_fraction(0.40, 0.60, Side.BUY, confidence=1.0, params=params)
    assert fraction == pytest.approx(1 / 3)


def test_kelly_fraction_matches_closed_form_for_sell():
    # f* = (price - fair) / price = (0.60 - 0.40) / 0.60 = 1/3
    params = KellyParams(kelly_fraction=1.0)
    fraction = kelly_stake_fraction(0.60, 0.40, Side.SELL, confidence=1.0, params=params)
    assert fraction == pytest.approx(1 / 3)


def test_kelly_fraction_is_zero_when_no_edge_in_requested_direction():
    params = KellyParams(kelly_fraction=1.0)
    # Fair price below current price is not an edge to BUY.
    assert kelly_stake_fraction(0.50, 0.40, Side.BUY, confidence=1.0, params=params) == 0.0
    # Fair price above current price is not an edge to SELL.
    assert kelly_stake_fraction(0.50, 0.60, Side.SELL, confidence=1.0, params=params) == 0.0


def test_kelly_fraction_scales_with_confidence():
    params = KellyParams(kelly_fraction=1.0)
    full_confidence = kelly_stake_fraction(0.40, 0.60, Side.BUY, confidence=1.0, params=params)
    half_confidence = kelly_stake_fraction(0.40, 0.60, Side.BUY, confidence=0.5, params=params)
    assert half_confidence == pytest.approx(full_confidence / 2)


def test_kelly_fraction_scales_with_kelly_fraction_param():
    quarter_kelly = KellyParams(kelly_fraction=0.25)
    full_kelly = KellyParams(kelly_fraction=1.0)
    quarter = kelly_stake_fraction(0.40, 0.60, Side.BUY, confidence=1.0, params=quarter_kelly)
    full = kelly_stake_fraction(0.40, 0.60, Side.BUY, confidence=1.0, params=full_kelly)
    assert quarter == pytest.approx(full * 0.25)


def test_kelly_fraction_clamps_to_max_stake_fraction():
    params = KellyParams(kelly_fraction=1.0, max_stake_fraction=0.1)
    fraction = kelly_stake_fraction(0.40, 0.99, Side.BUY, confidence=1.0, params=params)
    assert fraction == 0.1


def test_kelly_fraction_is_zero_at_degenerate_buy_price():
    # BUY divides by (1 - price): undefined/degenerate only at price == 1.0.
    params = KellyParams(kelly_fraction=1.0)
    assert kelly_stake_fraction(1.0, 1.0, Side.BUY, confidence=1.0, params=params) == 0.0


def test_kelly_fraction_at_zero_buy_price_is_full_kelly():
    # price == 0.0 with any positive fair price is a free-money arb - full
    # Kelly correctly says bet everything (f* == 1.0), not a degenerate case.
    params = KellyParams(kelly_fraction=1.0)
    assert kelly_stake_fraction(0.0, 1.0, Side.BUY, confidence=1.0, params=params) == 1.0


def test_kelly_fraction_is_zero_at_degenerate_sell_price():
    # SELL divides by price: undefined/degenerate only at price == 0.0.
    params = KellyParams(kelly_fraction=1.0)
    assert kelly_stake_fraction(0.0, 0.0, Side.SELL, confidence=1.0, params=params) == 0.0


def test_kelly_fraction_at_full_sell_price_is_full_kelly():
    # price == 1.0 with fair == 0.0 is a free-money arb on the sell side.
    params = KellyParams(kelly_fraction=1.0)
    assert kelly_stake_fraction(1.0, 0.0, Side.SELL, confidence=1.0, params=params) == 1.0


# --- kelly_position_size_usd: full pipeline --------------------------------------


def test_kelly_position_size_usd_combines_fraction_and_bankroll():
    params = KellyParams(kelly_fraction=1.0)
    size = kelly_position_size_usd(
        current_price=0.40,
        edge_estimate=0.20,  # fair = 0.60
        side=Side.BUY,
        confidence=1.0,
        bankroll_usd=300.0,
        params=params,
    )
    assert size == pytest.approx((1 / 3) * 300.0)


def test_kelly_position_size_usd_is_zero_with_no_edge():
    size = kelly_position_size_usd(
        current_price=0.50,
        edge_estimate=-0.05,  # implies fair < current -> no edge to buy
        side=Side.BUY,
        confidence=1.0,
        bankroll_usd=300.0,
    )
    assert size == 0.0


def test_kelly_position_size_usd_is_zero_with_non_positive_bankroll():
    size = kelly_position_size_usd(
        current_price=0.40,
        edge_estimate=0.20,
        side=Side.BUY,
        confidence=1.0,
        bankroll_usd=0.0,
    )
    assert size == 0.0


@pytest.mark.parametrize("price", [0.0, 1.0, -0.1, 1.1])
def test_kelly_position_size_usd_is_zero_for_out_of_range_price(price):
    size = kelly_position_size_usd(
        current_price=price,
        edge_estimate=0.5,
        side=Side.BUY,
        confidence=1.0,
        bankroll_usd=300.0,
    )
    assert size == 0.0


def test_kelly_position_size_usd_never_exceeds_bankroll_scaled_by_max_stake_fraction():
    params = KellyParams(kelly_fraction=1.0, max_stake_fraction=0.2)
    size = kelly_position_size_usd(
        current_price=0.01,
        edge_estimate=0.98,  # huge edge - should clamp, not blow past max_stake_fraction
        side=Side.BUY,
        confidence=1.0,
        bankroll_usd=1000.0,
        params=params,
    )
    assert size == pytest.approx(200.0)
