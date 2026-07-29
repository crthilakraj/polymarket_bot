"""Tracks cash, positions, and realized P&L as fills are applied during a
backtest replay. Standard weighted-average-cost accounting, symmetric for
long and short positions (a SELL fill against no position opens a short; a
BUY fill against a short covers it, realizing P&L on the covered portion).
"""

from dataclasses import dataclass
from datetime import datetime

from signals.base import Side


@dataclass
class Position:
    token_id: str
    condition_id: str | None
    shares: float = 0.0
    avg_cost: float = 0.0


@dataclass(frozen=True)
class Fill:
    token_id: str
    condition_id: str | None
    side: Side
    price: float
    size: float
    timestamp: datetime
    strategy: str


class Portfolio:
    def __init__(self, initial_cash: float):
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.realized_pnl = 0.0
        self.positions: dict[str, Position] = {}
        self.fills: list[Fill] = []

    def apply_fill(
        self,
        token_id: str,
        condition_id: str | None,
        side: Side,
        price: float,
        size: float,
        timestamp: datetime,
        strategy: str,
        fee_rate: float = 0.0,
    ) -> None:
        """fee_rate is a fraction of this fill's notional (e.g. 0.02 = 2%,
        see SignalStrategy.fee_rate_for) - folded into an
        effective price so it hits both cash (immediately) and avg_cost /
        realized_pnl (an opening fee raises cost basis and so reduces
        unrealized_pnl until recovered; a closing fee reduces realized_pnl
        directly), rather than being tracked as a separate side-channel that
        the rest of the accounting would need to remember to consult."""
        if size <= 0:
            raise ValueError(f"fill size must be positive, got {size}")

        fee_per_share = price * fee_rate
        # A fee is a cost regardless of side: buying pays more than the
        # quoted price effectively, selling receives less.
        effective_price = price + fee_per_share if side is Side.BUY else price - fee_per_share

        position = self.positions.setdefault(token_id, Position(token_id, condition_id))
        signed_size = size if side is Side.BUY else -size
        self.cash -= price * signed_size
        self.cash -= fee_per_share * size

        extending = position.shares == 0 or (position.shares > 0) == (signed_size > 0)
        if extending:
            new_shares = position.shares + signed_size
            total_cost = position.avg_cost * position.shares + effective_price * signed_size
            position.avg_cost = total_cost / new_shares if new_shares != 0 else 0.0
            position.shares = new_shares
        else:
            closing_size = min(abs(signed_size), abs(position.shares))
            direction = 1 if position.shares > 0 else -1
            self.realized_pnl += (effective_price - position.avg_cost) * closing_size * direction
            remaining = abs(signed_size) - closing_size
            position.shares += signed_size
            if remaining > 0:
                position.avg_cost = effective_price  # flipped through zero - fresh position here
            elif position.shares == 0:
                position.avg_cost = 0.0

        self.fills.append(Fill(token_id, condition_id, side, price, size, timestamp, strategy))

    def mark_to_market(self, latest_prices: dict[str, float]) -> float:
        """Total equity: cash plus every open position valued at
        latest_prices (falling back to the position's own avg_cost if no
        current price is known for that token)."""
        equity = self.cash
        for token_id, position in self.positions.items():
            price = latest_prices.get(token_id, position.avg_cost)
            equity += position.shares * price
        return equity

    def settle(self, resolutions: dict[str, float]) -> None:
        """Convert resolved positions (token_id -> terminal payout, 1.0 or
        0.0) into cash and realized P&L. Positions not covered by
        `resolutions` are left open for the caller to mark-to-market instead."""
        for token_id, payout in resolutions.items():
            position = self.positions.get(token_id)
            if position is None or position.shares == 0:
                continue
            self.cash += payout * position.shares
            self.realized_pnl += (payout - position.avg_cost) * position.shares
            position.shares = 0.0
            position.avg_cost = 0.0
