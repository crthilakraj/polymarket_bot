from dataclasses import dataclass

import pytest

from signals.base import Side
from signals.market_making.models import Inventory, PositionLimits


def test_inventory_apply_fill_buy_increases_position():
    inventory = Inventory(token_id="t1")
    inventory.apply_fill(Side.BUY, 5.0)
    assert inventory.position == 5.0


def test_inventory_apply_fill_sell_decreases_position():
    inventory = Inventory(token_id="t1", position=5.0)
    inventory.apply_fill(Side.SELL, 3.0)
    assert inventory.position == 2.0


def test_inventory_apply_fill_can_go_short():
    inventory = Inventory(token_id="t1")
    inventory.apply_fill(Side.SELL, 4.0)
    assert inventory.position == -4.0


def test_inventory_apply_fill_rejects_negative_size():
    inventory = Inventory(token_id="t1")
    with pytest.raises(ValueError):
        inventory.apply_fill(Side.BUY, -1.0)


def test_position_limits_rejects_non_positive_values():
    with pytest.raises(ValueError):
        PositionLimits(max_position=0.0, max_order_size=10.0)
    with pytest.raises(ValueError):
        PositionLimits(max_position=10.0, max_order_size=0.0)


@dataclass
class _FakeSettings:
    max_position_usd: float
    max_order_usd: float


def test_position_limits_from_settings_reuses_generic_risk_limits():
    settings = _FakeSettings(max_position_usd=100.0, max_order_usd=25.0)

    limits = PositionLimits.from_settings(settings)

    assert limits.max_position == 100.0
    assert limits.max_order_size == 25.0
