"""OrderManager: the single risk-gated entry point every strategy's output
routes through before anything reaches the exchange.

Three entry points, because strategy outputs don't all produce the same
shape of decision:

  - handle_signal(signal, market, order_book): for single-token
    Signal-producing strategies (signals.news) - sizes the trade with
    fractional Kelly (execution.sizing), then runs it through the risk gate.
  - handle_multi_leg_signal(signal, market): for multi-token/arb Signal
    strategies (signals.complementary_outcomes) - a complete-set arb needs
    equal SHARE counts across every leg (not equal dollar notional, since leg
    prices differ), which is a different sizing rule from Kelly. Sizes the
    whole basket once against the risk gate, then submits one same-share-count
    leg per token.
  - handle_quote(quote_pair, market): for MarketMakingStrategy's two-sided
    quotes, which are already sized by its own position caps rather than
    Kelly (there's no single "edge estimate" to size against - it's
    continuous two-sided liquidity provision, not a one-off directional bet).

All three converge on _submit(), the actual shared gate: idempotency check,
then submit (or just log, in dry-run mode) and book exposure. This is what
"every signal type routes through the same risk gate" means concretely - not
that they share one method signature (a Signal, a multi-leg Signal, and a
QuotePair are genuinely different shapes), but that every order from every
strategy passes through the same check_order() call (once per basket for
multi-leg, once per order otherwise) and the same exposure bookkeeping
before anything is sent.

Exposure is tracked as notional committed at *submission* time (price *
size of every order OrderManager sends), not at fill time - on Polymarket,
placing a limit order locks collateral immediately, so this is a reasonable
proxy for capital at risk. Dry-run decisions book exposure too, so caps are
exercised the same way whether or not orders actually go out; it does not
currently decrease when an order is later cancelled - that's a documented
gap, not an oversight (see README).
"""

import dataclasses
import hashlib
import logging
import time
from collections.abc import Callable, Iterable

from data.models import MarketMetadata, OrderBook
from execution import orders as orders_module
from execution.models import OrderDecision, OrderIntent, OrderStatus
from execution.risk import RiskLimits, check_order
from execution.sizing import KellyParams, kelly_position_size_usd
from signals.base import Side, Signal
from signals.market_making.models import QuotePair

logger = logging.getLogger(__name__)

DEFAULT_IDEMPOTENCY_TTL_SECONDS = 60.0


class OrderManager:
    def __init__(
        self,
        risk_limits: RiskLimits,
        client=None,
        dry_run: bool = True,
        kelly_params: KellyParams = KellyParams(),
        idempotency_ttl_seconds: float = DEFAULT_IDEMPOTENCY_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        live_balance_fn: Callable[[], float] | None = None,
        live_balance_cache_seconds: float = 30.0,
    ) -> None:
        """`clock` drives the idempotency TTL window - defaults to real wall
        time for live trading. backtest.engine passes a clock tied to the
        simulated event timestamp being replayed instead: real wall time
        would make the 60s dedup window measure how long the REPLAY
        COMPUTATION takes rather than how far apart the historical events
        actually were, silently making backtest fills/PnL depend on how fast
        the machine running the backtest happens to be - a real
        reproducibility bug, not just an inaccuracy (see README).

        `live_balance_fn`, if given, is called (with `_live_balance_cache_seconds`
        caching, keyed off `clock`) to fetch the real USDC collateral balance
        for the trading wallet - see execution.client.get_collateral_balance_usd.
        When set, every risk-gate check further caps the portfolio exposure
        ceiling to the live balance, so sizing can never assume more real
        cash is available than the wallet actually holds. Left None (the
        default, and always the case for dry-run) means exposure is capped
        purely by the configured RiskLimits, with no live-funds awareness."""
        self._risk_limits = risk_limits
        self._client = client
        self._dry_run = dry_run
        self._kelly_params = kelly_params
        self._idempotency_ttl_seconds = idempotency_ttl_seconds
        self._clock = clock
        self._live_balance_fn = live_balance_fn
        self._live_balance_cache_seconds = live_balance_cache_seconds
        self._live_balance_cache: tuple[float, float] | None = None
        self._market_exposure_usd: dict[str, float] = {}
        self._total_exposure_usd = 0.0
        self._recent_intents: dict[str, float] = {}

    # ---- exposure bookkeeping ---------------------------------------------------

    def market_exposure(self, condition_id: str) -> float:
        return self._market_exposure_usd.get(condition_id, 0.0)

    @property
    def total_exposure(self) -> float:
        return self._total_exposure_usd

    def _record_exposure(self, condition_id: str, notional_usd: float) -> None:
        self._market_exposure_usd[condition_id] = self.market_exposure(condition_id) + notional_usd
        self._total_exposure_usd += notional_usd

    # ---- live-funds awareness -----------------------------------------------------

    def _live_balance_usd(self) -> float | None:
        """Cached fetch of the real wallet balance. Returns None if no
        live_balance_fn was configured (dry-run, or a live client without one
        wired up). A fetch failure logs loudly and falls back to the last
        known good value rather than raising - a transient balance-API
        hiccup shouldn't halt trading entirely, but it must never be
        silently swallowed, since that would defeat the point of this check."""
        if self._live_balance_fn is None:
            return None
        now = self._clock()
        if self._live_balance_cache is not None:
            value, fetched_at = self._live_balance_cache
            if now - fetched_at < self._live_balance_cache_seconds:
                return value
        try:
            value = self._live_balance_fn()
        except Exception:
            logger.exception(
                "failed to fetch live USDC balance - falling back to last known value (or the "
                "configured cap alone, if no fetch has ever succeeded)"
            )
            return self._live_balance_cache[0] if self._live_balance_cache is not None else None
        self._live_balance_cache = (value, now)
        return value

    def _effective_risk_limits(self) -> RiskLimits:
        """self._risk_limits with max_portfolio_exposure_usd further capped
        to the real live balance, if live-funds awareness is configured and
        the wallet currently holds less than the configured ceiling. This is
        what makes check_order() actually reject/resize orders against real
        available funds, instead of only ever trusting a static .env cap."""
        live_balance = self._live_balance_usd()
        if live_balance is None or live_balance >= self._risk_limits.max_portfolio_exposure_usd:
            return self._risk_limits
        logger.warning(
            "live USDC balance ($%.2f) is below the configured cap ($%.2f) - "
            "capping portfolio exposure to the live balance",
            live_balance,
            self._risk_limits.max_portfolio_exposure_usd,
        )
        return dataclasses.replace(self._risk_limits, max_portfolio_exposure_usd=max(0.0, live_balance))

    # ---- signal-driven path (single-token, e.g. news) ----------------------------

    def handle_signal(
        self, signal: Signal, market: MarketMetadata, order_book: OrderBook
    ) -> OrderDecision:
        if signal.token_id is None:
            return OrderDecision(
                status=OrderStatus.REJECTED,
                intent=None,
                reasons=["signal has no token_id - use handle_multi_leg_signal() instead"],
            )

        current_price = self._transacting_price(order_book, signal.side)
        if current_price is None:
            return OrderDecision(
                status=OrderStatus.REJECTED,
                intent=None,
                reasons=["no price available on the relevant side of the book"],
            )

        requested_usd = kelly_position_size_usd(
            current_price=current_price,
            edge_estimate=signal.edge_estimate,
            side=signal.side,
            confidence=signal.confidence,
            bankroll_usd=self._risk_limits.max_portfolio_exposure_usd,
            params=self._kelly_params,
        )
        if requested_usd <= 0:
            return OrderDecision(
                status=OrderStatus.REJECTED,
                intent=None,
                reasons=["Kelly sizing produced zero size for this signal"],
            )

        return self._evaluate_and_submit(
            condition_id=market.condition_id,
            token_id=signal.token_id,
            side=signal.side,
            price=current_price,
            requested_usd=requested_usd,
            strategy="signal",
        )

    # ---- multi-leg signal-driven path (e.g. complementary_outcomes arb) ----------

    def handle_multi_leg_signal(self, signal: Signal, market: MarketMetadata) -> list[OrderDecision]:
        """signal.metadata["legs"] must be a list of {"token_id", "side", "price"}
        dicts (one per outcome) - see signals/complementary_outcomes.py. Sizes
        the whole complete-set basket against the risk gate once, then submits
        one leg per token, all at the same share count."""
        legs = signal.metadata.get("legs")
        if not legs:
            return [
                OrderDecision(
                    status=OrderStatus.REJECTED, intent=None, reasons=["signal has no legs to submit"]
                )
            ]

        condition_id = market.condition_id
        basket_cost_per_set = sum(leg["price"] for leg in legs)
        if basket_cost_per_set <= 0:
            return [
                OrderDecision(
                    status=OrderStatus.REJECTED,
                    intent=None,
                    reasons=["basket cost per complete set is zero or negative"],
                )
            ]
        # fee_cost (dollars per complete set, from ComplementaryOutcomesSignal)
        # must be included when sizing against the exposure budget, or actual
        # cash spent (notional + fee) can exceed the approved notional once
        # the portfolio is near-fully deployed - a real bug found live (cash
        # went slightly negative because sizing ignored the fee entirely).
        effective_cost_per_set = basket_cost_per_set + signal.metadata.get("fee_cost", 0.0)

        result = check_order(
            requested_size_usd=self._risk_limits.max_order_usd,
            current_market_exposure_usd=self.market_exposure(condition_id) if condition_id else 0.0,
            current_total_exposure_usd=self.total_exposure,
            limits=self._effective_risk_limits(),
        )
        if not result.approved:
            logger.info(
                "multi-leg order rejected for %s: %s", condition_id, "; ".join(result.reasons)
            )
            return [OrderDecision(status=OrderStatus.REJECTED, intent=None, reasons=result.reasons)]

        num_complete_sets = result.approved_size_usd / effective_cost_per_set
        max_shares = signal.metadata.get("max_shares")
        if max_shares is not None and max_shares < num_complete_sets:
            num_complete_sets = max_shares
        if num_complete_sets <= 0:
            return [
                OrderDecision(
                    status=OrderStatus.REJECTED,
                    intent=None,
                    reasons=["no size available at quoted price (book depth exhausted)"],
                )
            ]
        # Recovers the fee rate from the two per-set costs above, so exposure
        # booked for this basket (see _submit) matches the true cash cost
        # (notional + fee) - not just recording it once at sizing time. The
        # sizing fix above alone wasn't enough: it kept any ONE trade from
        # overshooting its own budget, but exposure bookkeeping still only
        # ever recorded raw notional, so cumulative unrecorded fees across
        # many trades let cash drift further negative over a session (found
        # live: -$3.31 became -$33.38 after the sizing-only fix).
        fee_rate = (effective_cost_per_set / basket_cost_per_set) - 1.0

        # _submit()'s idempotency check operates per order, keyed on
        # (token_id, side, price, size, strategy). If checked independently
        # per leg, a leg whose price happens to repeat from a recent
        # submission gets silently deduped while its sibling leg (a
        # different price, so not a duplicate) goes through - leaving a
        # real, unhedged single-leg position instead of the complete-set
        # basket this strategy depends on for its risk profile. Found live:
        # one outcome's book didn't move between two ticks while the other
        # did, and the basket ended up with mismatched share counts on its
        # two legs (263.62 vs 328.30). Check every leg's key upfront and
        # reject the WHOLE basket if any leg would be a duplicate, instead
        # of submitting a subset of legs.
        leg_keys = [
            self._idempotency_key(leg["token_id"], leg["side"], leg["price"], num_complete_sets, "signal_multi_leg")
            for leg in legs
        ]
        if any(self._is_duplicate(key) for key in leg_keys):
            logger.info(
                "rejecting entire multi-leg basket for %s: at least one leg duplicates a recently-submitted intent",
                condition_id,
            )
            return [
                OrderDecision(
                    status=OrderStatus.REJECTED,
                    intent=None,
                    reasons=["duplicate leg detected in basket - rejecting entire basket to avoid unhedged exposure"],
                )
                for _ in legs
            ]

        return [
            self._submit(
                condition_id=condition_id,
                token_id=leg["token_id"],
                side=leg["side"],
                price=leg["price"],
                size=num_complete_sets,
                strategy="signal_multi_leg",
                reasons=result.reasons,
                fee_rate=fee_rate,
            )
            for leg in legs
        ]

    # ---- quote-driven path (market making) ---------------------------------------

    def handle_quote(self, quote_pair: QuotePair, market: MarketMetadata) -> list[OrderDecision]:
        decisions = []
        for side, quote in ((Side.BUY, quote_pair.bid), (Side.SELL, quote_pair.ask)):
            if quote is None:
                continue
            decisions.append(
                self._evaluate_and_submit(
                    condition_id=market.condition_id,
                    token_id=quote_pair.token_id,
                    side=side,
                    price=quote.price,
                    requested_usd=quote.price * quote.size,
                    strategy="market_making",
                )
            )
        return decisions

    # ---- shared risk gate + submission --------------------------------------------

    def _evaluate_and_submit(
        self,
        *,
        condition_id: str | None,
        token_id: str,
        side: Side,
        price: float,
        requested_usd: float,
        strategy: str,
    ) -> OrderDecision:
        result = check_order(
            requested_size_usd=requested_usd,
            current_market_exposure_usd=self.market_exposure(condition_id) if condition_id else 0.0,
            current_total_exposure_usd=self.total_exposure,
            limits=self._effective_risk_limits(),
        )
        if not result.approved:
            logger.info(
                "order rejected for %s/%s (%s): %s",
                condition_id,
                token_id,
                strategy,
                "; ".join(result.reasons),
            )
            return OrderDecision(status=OrderStatus.REJECTED, intent=None, reasons=result.reasons)

        size = result.approved_size_usd / price
        return self._submit(
            condition_id=condition_id,
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            strategy=strategy,
            reasons=result.reasons,
        )

    def _submit(
        self,
        *,
        condition_id: str | None,
        token_id: str,
        side: Side,
        price: float,
        size: float,
        strategy: str,
        reasons: Iterable[str] = (),
        fee_rate: float = 0.0,
    ) -> OrderDecision:
        """Idempotency check, then submit (or log, in dry-run mode) and book
        exposure. Assumes sizing/risk-approval already happened - callers are
        _evaluate_and_submit() (one order) and handle_multi_leg_signal()
        (several legs sharing one upfront risk check)."""
        reasons = list(reasons)
        idempotency_key = self._idempotency_key(token_id, side, price, size, strategy)
        intent = OrderIntent(
            token_id=token_id,
            condition_id=condition_id,
            side=side,
            price=price,
            size=size,
            strategy=strategy,
            idempotency_key=idempotency_key,
        )

        if self._is_duplicate(idempotency_key):
            logger.info(
                "skipping duplicate order intent %s (seen within %.0fs)",
                idempotency_key,
                self._idempotency_ttl_seconds,
            )
            return OrderDecision(
                status=OrderStatus.REJECTED,
                intent=intent,
                reasons=["duplicate of a recently-submitted intent"],
            )

        notional_usd = price * size * (1.0 + fee_rate)

        if self._dry_run:
            logger.info(
                "[DRY RUN] would place %s %s %.4f shares @ %.4f (~$%.2f) on token=%s market=%s",
                strategy,
                side.value,
                size,
                price,
                notional_usd,
                token_id,
                condition_id,
            )
            self._mark_seen(idempotency_key)
            # Still book the exposure in dry-run mode: the point of dry-run is
            # to exercise the risk gate faithfully (including caps kicking in
            # across a sequence of decisions), just without sending anything.
            if condition_id:
                self._record_exposure(condition_id, notional_usd)
            return OrderDecision(status=OrderStatus.DRY_RUN, intent=intent, reasons=reasons)

        if self._client is None:
            raise RuntimeError("OrderManager has no client configured and dry_run is False")

        try:
            response = orders_module.place_order(self._client, intent)
        except orders_module.OrderPlacementError:
            logger.exception("order placement failed for %s", idempotency_key)
            return OrderDecision(
                status=OrderStatus.FAILED,
                intent=intent,
                reasons=["order placement failed - see logs"],
            )

        self._mark_seen(idempotency_key)
        if condition_id:
            self._record_exposure(condition_id, notional_usd)
        order_id = response.get("orderID") if isinstance(response, dict) else None
        return OrderDecision(status=OrderStatus.SUBMITTED, intent=intent, reasons=reasons, order_id=order_id)

    def _is_duplicate(self, idempotency_key: str) -> bool:
        seen_at = self._recent_intents.get(idempotency_key)
        if seen_at is None:
            return False
        return (self._clock() - seen_at) < self._idempotency_ttl_seconds

    def _mark_seen(self, idempotency_key: str) -> None:
        self._recent_intents[idempotency_key] = self._clock()

    @staticmethod
    def _idempotency_key(token_id: str, side: Side, price: float, size: float, strategy: str) -> str:
        raw = f"{token_id}|{side.value}|{price:.4f}|{size:.4f}|{strategy}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    @staticmethod
    def _transacting_price(order_book: OrderBook, side: Side) -> float | None:
        """The price an order on this side would actually transact at:
        buying pays the ask, selling hits the bid."""
        if side is Side.BUY:
            best_ask = order_book.best_ask
            return best_ask.price if best_ask else None
        best_bid = order_book.best_bid
        return best_bid.price if best_bid else None
