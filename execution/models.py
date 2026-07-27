"""Data shapes shared across execution/."""

from dataclasses import dataclass, field
from enum import Enum

from signals.base import Side


class OrderStatus(str, Enum):
    SUBMITTED = "SUBMITTED"
    """Sent to the exchange and accepted."""

    DRY_RUN = "DRY_RUN"
    """Would have been sent, but dry_run mode is on - nothing was submitted."""

    REJECTED = "REJECTED"
    """Never sent - rejected by our own risk gate, sizing, or idempotency check."""

    FAILED = "FAILED"
    """Sent, but placement failed even after retries (network/exchange error)."""

    @property
    def was_sent(self) -> bool:
        return self is OrderStatus.SUBMITTED


@dataclass(frozen=True)
class OrderIntent:
    """A fully-specified, risk-approved order OrderManager is about to place
    (or would place, in dry-run mode)."""

    token_id: str
    condition_id: str | None
    side: Side
    price: float
    size: float
    strategy: str
    idempotency_key: str


@dataclass(frozen=True)
class OrderDecision:
    """The outcome of OrderManager processing one signal or quote leg."""

    status: OrderStatus
    intent: OrderIntent | None
    reasons: list[str] = field(default_factory=list)
    order_id: str | None = None
