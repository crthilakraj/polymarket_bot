import pytest

from signals.base import Side, Signal, SignalStrategy


def test_signal_strategy_is_abstract():
    with pytest.raises(TypeError):
        SignalStrategy()


def test_signal_rejects_confidence_outside_unit_interval():
    with pytest.raises(ValueError):
        Signal(edge_estimate=0.05, confidence=1.5, side=Side.BUY)

    with pytest.raises(ValueError):
        Signal(edge_estimate=0.05, confidence=-0.1, side=Side.BUY)


def test_signal_accepts_boundary_confidence_values():
    Signal(edge_estimate=0.05, confidence=0.0, side=Side.BUY)
    Signal(edge_estimate=0.05, confidence=1.0, side=Side.SELL)
