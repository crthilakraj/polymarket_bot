"""Pre-trade risk gate: hard per-market and total portfolio exposure caps.

Pure and stateless - given the exposure already committed and a proposed
order's notional, decides whether to approve it as-is, resize it down, or
reject it outright. Exposure bookkeeping (how much is currently committed per
market and in total) lives in OrderManager; this module only judges a single
proposal against the limits, which is what makes it independently testable
without any OrderManager/exchange state involved.

Every order - regardless of which strategy produced it - passes through
check_order() before OrderManager will submit it, so all signal types are
held to the same hard caps.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RiskLimits:
    max_position_usd: float
    """Hard cap on total notional committed to a single market (condition_id)."""

    max_order_usd: float
    """Hard cap on a single order's notional."""

    max_portfolio_exposure_usd: float
    """Hard cap on total notional committed across all markets."""

    def __post_init__(self) -> None:
        for name in ("max_position_usd", "max_order_usd", "max_portfolio_exposure_usd"):
            value = getattr(self, name)
            if value <= 0:
                raise ValueError(f"{name} must be positive, got {value}")

    @classmethod
    def from_settings(cls, settings) -> "RiskLimits":
        return cls(
            max_position_usd=settings.max_position_usd,
            max_order_usd=settings.max_order_usd,
            max_portfolio_exposure_usd=settings.max_portfolio_exposure_usd,
        )


@dataclass(frozen=True)
class RiskCheckResult:
    approved_size_usd: float
    reasons: list[str] = field(default_factory=list)

    @property
    def approved(self) -> bool:
        return self.approved_size_usd > 0.0

    @property
    def resized(self) -> bool:
        return self.approved and bool(self.reasons)


def check_order(
    requested_size_usd: float,
    current_market_exposure_usd: float,
    current_total_exposure_usd: float,
    limits: RiskLimits,
) -> RiskCheckResult:
    """Approve, resize, or reject requested_size_usd against the hard caps in
    limits, given exposure already committed. Caps are applied in order
    (single-order cap, then per-market room, then portfolio room), each
    potentially shrinking the size further - never growing it."""
    if requested_size_usd <= 0:
        return RiskCheckResult(approved_size_usd=0.0, reasons=["requested size is zero or negative"])

    size = requested_size_usd
    reasons: list[str] = []

    if size > limits.max_order_usd:
        size = limits.max_order_usd
        reasons.append(f"resized to max_order_usd (${limits.max_order_usd:.2f})")

    market_room = max(0.0, limits.max_position_usd - current_market_exposure_usd)
    if size > market_room:
        size = market_room
        reasons.append(f"resized to remaining per-market room (${market_room:.2f})")

    portfolio_room = max(0.0, limits.max_portfolio_exposure_usd - current_total_exposure_usd)
    if size > portfolio_room:
        size = portfolio_room
        reasons.append(f"resized to remaining portfolio room (${portfolio_room:.2f})")

    if size <= 0:
        return RiskCheckResult(
            approved_size_usd=0.0, reasons=reasons + ["no room left under exposure caps"]
        )

    return RiskCheckResult(approved_size_usd=size, reasons=reasons)
