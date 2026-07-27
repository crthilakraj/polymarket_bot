"""Data shapes for the market-making strategy."""

from dataclasses import dataclass

from signals.base import Side


@dataclass(frozen=True)
class Quote:
    price: float
    size: float


@dataclass(frozen=True)
class QuotePair:
    """Both sides of a market maker's quote for one token. Either side may be
    None when a hard position cap leaves no room to quote it."""

    token_id: str
    bid: Quote | None
    ask: Quote | None


@dataclass
class Inventory:
    """A market maker's position in a single token. Positive = long, negative = short."""

    token_id: str
    position: float = 0.0

    def apply_fill(self, side: Side, size: float) -> None:
        if size < 0:
            raise ValueError(f"fill size must be non-negative, got {size}")
        if side is Side.BUY:
            self.position += size
        else:
            self.position -= size


@dataclass(frozen=True)
class PositionLimits:
    """Hard caps enforced by MarketMakingStrategy - quoting a side stops (or
    shrinks) once filling it further would breach these."""

    max_position: float
    max_order_size: float

    def __post_init__(self) -> None:
        if self.max_position <= 0:
            raise ValueError(f"max_position must be positive, got {self.max_position}")
        if self.max_order_size <= 0:
            raise ValueError(f"max_order_size must be positive, got {self.max_order_size}")

    @classmethod
    def from_settings(cls, settings) -> "PositionLimits":
        """Build from config.Settings' generic risk limits (max_position_usd,
        max_order_usd) - reused here rather than adding market-making-specific
        config, since a share of a $0-$1 prediction market outcome is already
        close to a dollar of notional."""
        return cls(max_position=settings.max_position_usd, max_order_size=settings.max_order_usd)
