"""Rolling volatility estimate: stdev of recent mid-price returns, feeding
spread.volatility_factor. Kept separate from MarketMakingStrategy so it has
its own state (one tracker per token) without complicating the strategy class.
"""

import math
from collections import deque


class RollingVolatility:
    """Tracks the last `window` mid-price updates for one token and reports
    the sample stdev of the returns between consecutive updates - a unitless
    measure comparable across markets/price levels."""

    def __init__(self, window: int = 20):
        if window < 2:
            raise ValueError("window must be at least 2")
        self._prices: deque[float] = deque(maxlen=window)

    def update(self, price: float) -> None:
        self._prices.append(price)

    def normalized_volatility(self) -> float:
        if len(self._prices) < 2:
            return 0.0
        prices = list(self._prices)
        returns = [
            (curr - prev) / prev for prev, curr in zip(prices, prices[1:]) if prev != 0
        ]
        if len(returns) < 2:
            return 0.0
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        return math.sqrt(variance)
