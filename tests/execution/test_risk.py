from dataclasses import dataclass

import pytest

from execution.risk import RiskLimits, check_order


def make_limits(**overrides) -> RiskLimits:
    defaults = dict(max_position_usd=100.0, max_order_usd=25.0, max_portfolio_exposure_usd=300.0)
    defaults.update(overrides)
    return RiskLimits(**defaults)


def test_risk_limits_rejects_non_positive_values():
    with pytest.raises(ValueError):
        RiskLimits(max_position_usd=0.0, max_order_usd=25.0, max_portfolio_exposure_usd=300.0)
    with pytest.raises(ValueError):
        RiskLimits(max_position_usd=100.0, max_order_usd=0.0, max_portfolio_exposure_usd=300.0)
    with pytest.raises(ValueError):
        RiskLimits(max_position_usd=100.0, max_order_usd=25.0, max_portfolio_exposure_usd=0.0)


@dataclass
class _FakeSettings:
    max_position_usd: float
    max_order_usd: float
    max_portfolio_exposure_usd: float


def test_risk_limits_from_settings():
    settings = _FakeSettings(max_position_usd=100.0, max_order_usd=25.0, max_portfolio_exposure_usd=300.0)
    limits = RiskLimits.from_settings(settings)
    assert limits.max_position_usd == 100.0
    assert limits.max_order_usd == 25.0
    assert limits.max_portfolio_exposure_usd == 300.0


def test_approves_a_request_within_all_limits():
    result = check_order(
        requested_size_usd=10.0,
        current_market_exposure_usd=0.0,
        current_total_exposure_usd=0.0,
        limits=make_limits(),
    )

    assert result.approved
    assert result.approved_size_usd == 10.0
    assert not result.resized


def test_rejects_zero_or_negative_request():
    result = check_order(
        requested_size_usd=0.0,
        current_market_exposure_usd=0.0,
        current_total_exposure_usd=0.0,
        limits=make_limits(),
    )

    assert not result.approved
    assert result.approved_size_usd == 0.0


def test_resizes_down_to_max_order_usd():
    result = check_order(
        requested_size_usd=50.0,
        current_market_exposure_usd=0.0,
        current_total_exposure_usd=0.0,
        limits=make_limits(max_order_usd=25.0),
    )

    assert result.approved
    assert result.approved_size_usd == 25.0
    assert result.resized


def test_resizes_down_to_remaining_per_market_room():
    result = check_order(
        requested_size_usd=25.0,
        current_market_exposure_usd=90.0,  # only $10 of room left under max_position_usd=100
        current_total_exposure_usd=90.0,
        limits=make_limits(),
    )

    assert result.approved
    assert result.approved_size_usd == 10.0


def test_rejects_when_market_is_already_at_its_cap():
    result = check_order(
        requested_size_usd=25.0,
        current_market_exposure_usd=100.0,
        current_total_exposure_usd=100.0,
        limits=make_limits(),
    )

    assert not result.approved
    assert result.approved_size_usd == 0.0


def test_resizes_down_to_remaining_portfolio_room():
    result = check_order(
        requested_size_usd=25.0,
        current_market_exposure_usd=0.0,
        current_total_exposure_usd=290.0,  # only $10 of room left under max_portfolio_exposure_usd=300
        limits=make_limits(),
    )

    assert result.approved
    assert result.approved_size_usd == 10.0


def test_rejects_when_portfolio_is_already_at_its_cap():
    result = check_order(
        requested_size_usd=25.0,
        current_market_exposure_usd=0.0,
        current_total_exposure_usd=300.0,
        limits=make_limits(),
    )

    assert not result.approved


def test_portfolio_cap_binds_tighter_than_market_cap_when_lower():
    # Per-market room is $50, but portfolio room is only $5 - the tighter cap wins.
    result = check_order(
        requested_size_usd=25.0,
        current_market_exposure_usd=50.0,
        current_total_exposure_usd=295.0,
        limits=make_limits(max_position_usd=100.0, max_portfolio_exposure_usd=300.0, max_order_usd=25.0),
    )

    assert result.approved_size_usd == 5.0
