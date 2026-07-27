import pytest

from signals.market_making.volatility import RollingVolatility


def test_rejects_window_smaller_than_two():
    with pytest.raises(ValueError):
        RollingVolatility(window=1)


def test_volatility_is_zero_with_fewer_than_two_updates():
    tracker = RollingVolatility(window=10)
    assert tracker.normalized_volatility() == 0.0

    tracker.update(0.5)
    assert tracker.normalized_volatility() == 0.0


def test_volatility_is_zero_for_constant_prices():
    tracker = RollingVolatility(window=10)
    for _ in range(5):
        tracker.update(0.5)

    assert tracker.normalized_volatility() == 0.0


def test_volatility_is_positive_for_varying_prices():
    tracker = RollingVolatility(window=10)
    for price in (0.5, 0.55, 0.48, 0.52, 0.60):
        tracker.update(price)

    assert tracker.normalized_volatility() > 0.0


def test_larger_price_swings_produce_higher_volatility():
    calm = RollingVolatility(window=10)
    for price in (0.50, 0.505, 0.498, 0.502):
        calm.update(price)

    choppy = RollingVolatility(window=10)
    for price in (0.50, 0.60, 0.40, 0.65):
        choppy.update(price)

    assert choppy.normalized_volatility() > calm.normalized_volatility()


def test_window_evicts_oldest_prices():
    tracker = RollingVolatility(window=3)
    for price in (0.5, 0.5, 0.5, 0.5, 0.5):
        tracker.update(price)

    # A big spike four updates ago should no longer affect volatility once
    # it's fallen out of a window of 3.
    tracker.update(0.9)
    tracker.update(0.5)
    tracker.update(0.5)
    tracker.update(0.5)

    assert tracker.normalized_volatility() == 0.0
