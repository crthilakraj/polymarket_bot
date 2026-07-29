"""Backtest engine: replays historical order book data (from data.store.DataStore,
via backtest.data_source) through the real signal and execution layers - no
live API calls anywhere in this loop.

Signal-producing strategies (signals.base.SignalStrategy - complementary
outcomes, news) go through execution.order_manager.OrderManager exactly as
they would live, in dry_run mode (so nothing ever reaches execution.orders /
py-clob-client); the resulting decisions are turned into simulated fills here.
MarketMakingStrategy, which isn't a SignalStrategy, goes through
OrderManager.handle_quote() and a simple one-step-lookahead crossing model:
a resting quote is treated as filled if the *next* snapshot for that token
crosses through it, before the strategy re-quotes.

Multi-leg signals (signal.token_id is None - e.g. ComplementaryOutcomesSignal's
complete-set arbs) go through OrderManager.handle_multi_leg_signal(), which
sizes the whole basket at equal share counts per leg rather than Kelly-sizing
each leg independently, but is still gated by the same exposure caps as
everything else the manager submits.

News signals are supported by the same generic loop (they're an ordinary
SignalStrategy), but will never actually fire here: NewsEdgeSignal.evaluate()
only produces a Signal when SignalContext.metadata["headline"] is set, and
this engine has no historical headline feed to supply one. That's a
documented gap (see README), not a bug - wire up your own headline replay
and call the news strategy separately if you need to backtest it.
"""

import logging
from collections.abc import Iterable
from datetime import datetime

from data.models import MarketMetadata, OrderBook
from data.store import DataStore
from execution.models import OrderStatus
from execution.order_manager import OrderManager
from execution.risk import RiskLimits
from execution.sizing import KellyParams, implied_fair_price
from signals.base import Side, SignalContext, SignalStrategy
from signals.market_making.models import Quote, QuotePair
from signals.market_making.strategy import MarketMakingStrategy

from backtest.data_source import HistoricalDataSource, infer_resolution
from backtest.portfolio import Portfolio
from backtest.report import BacktestResult, Prediction, build_result

logger = logging.getLogger(__name__)

DEFAULT_INITIAL_CASH = 1000.0

Strategy = SignalStrategy | MarketMakingStrategy


class _SimulatedClock:
    """A zero-arg callable clock for OrderManager's idempotency TTL, driven
    by the timestamp of the historical event currently being replayed rather
    than real wall time - see OrderManager.__init__'s `clock` docstring for
    why real time would make backtest results depend on how fast the replay
    computation runs, not on the actual simulated event spacing."""

    def __init__(self) -> None:
        self._now = 0.0

    def advance(self, timestamp: datetime) -> None:
        self._now = timestamp.timestamp()

    def __call__(self) -> float:
        return self._now


def run_backtest(
    strategies: dict[str, Strategy],
    condition_ids: list[str],
    db_path: str,
    start: datetime | None = None,
    end: datetime | None = None,
    risk_limits: RiskLimits | None = None,
    kelly_params: KellyParams = KellyParams(),
    initial_cash: float = DEFAULT_INITIAL_CASH,
    mode: str = "isolated",
) -> dict[str, BacktestResult] | BacktestResult:
    """Replay stored history for condition_ids (optionally bounded to
    [start, end]) through `strategies`.

    mode="isolated" (default): each strategy gets its own fresh portfolio and
    OrderManager, replaying independently - lets you compare strategies'
    standalone performance. Returns {name: BacktestResult}.

    mode="combined": every strategy replays through ONE shared OrderManager
    and portfolio, competing for the same exposure caps exactly as they
    would live. Returns a single BacktestResult; per-strategy attribution is
    available via each Fill.strategy in result.fills.
    """
    if mode not in ("isolated", "combined"):
        raise ValueError(f"mode must be 'isolated' or 'combined', got {mode!r}")
    if not strategies:
        raise ValueError("strategies must be non-empty")

    risk_limits = risk_limits or RiskLimits(
        max_position_usd=initial_cash,
        max_order_usd=initial_cash,
        max_portfolio_exposure_usd=initial_cash,
    )

    store = DataStore(db_path)
    try:
        data_source = HistoricalDataSource(store)
        if mode == "isolated":
            return {
                name: _run_single(
                    {name: strategy},
                    data_source,
                    condition_ids,
                    start,
                    end,
                    risk_limits,
                    kelly_params,
                    initial_cash,
                )
                for name, strategy in strategies.items()
            }
        return _run_single(
            strategies, data_source, condition_ids, start, end, risk_limits, kelly_params, initial_cash
        )
    finally:
        store.close()


def _run_single(
    strategies: dict[str, Strategy],
    data_source: HistoricalDataSource,
    condition_ids: list[str],
    start: datetime | None,
    end: datetime | None,
    risk_limits: RiskLimits,
    kelly_params: KellyParams,
    initial_cash: float,
) -> BacktestResult:
    markets = data_source.load_markets(condition_ids)
    portfolio = Portfolio(initial_cash)
    sim_clock = _SimulatedClock()
    order_manager = OrderManager(
        risk_limits=risk_limits, dry_run=True, kelly_params=kelly_params, clock=sim_clock
    )

    latest_books: dict[str, OrderBook] = {}
    latest_prices: dict[str, float] = {}
    open_mm_quotes: dict[str, QuotePair] = {}
    equity_curve: list[tuple[datetime, float]] = []
    predictions: list[Prediction] = []

    for event in data_source.replay_events(markets, start=start, end=end):
        book = event.order_book
        market = event.market
        sim_clock.advance(book.received_at)
        latest_books[book.token_id] = book
        # Deliberately stricter than OrderBook.mid_price (which falls back to
        # a lone one-sided quote): a book with only a bid or only an ask is
        # often a stale/thin resting order rather than a live two-sided
        # price - trusting it for mark-to-market can badly overstate
        # unrealized P&L right as a market's book empties out near
        # resolution (a real case found live: a losing outcome's last
        # snapshot before its book went fully empty was an erroneous
        # ask=0.999, which isn't what the market actually believed - see
        # README). Only a genuine two-sided quote updates latest_prices;
        # otherwise the last trusted price is carried forward unchanged.
        if book.best_bid is not None and book.best_ask is not None:
            latest_prices[book.token_id] = book.mid_price

        for name, strategy in strategies.items():
            if isinstance(strategy, MarketMakingStrategy):
                _process_market_making(name, strategy, market, book, order_manager, portfolio, open_mm_quotes)
            else:
                _process_signal_strategy(
                    name, strategy, market, book, latest_books, order_manager, portfolio, predictions
                )

        equity_curve.append((book.received_at, portfolio.mark_to_market(latest_prices)))

    resolutions = _resolve_markets(markets.values())
    portfolio.settle(resolutions)
    if equity_curve:
        equity_curve.append((equity_curve[-1][0], portfolio.mark_to_market(latest_prices)))

    graded_predictions = [
        (prediction.probability, resolutions[prediction.token_id])
        for prediction in predictions
        if prediction.token_id in resolutions
    ]

    return build_result(
        strategy_names=list(strategies.keys()),
        portfolio=portfolio,
        equity_curve=equity_curve,
        graded_predictions=graded_predictions,
    )


def _resolve_markets(markets: Iterable[MarketMetadata]) -> dict[str, float]:
    resolutions: dict[str, float] = {}
    for market in markets:
        market_resolutions = infer_resolution(market)
        if market_resolutions:
            resolutions.update(market_resolutions)
    return resolutions


# ---- Signal-producing strategies (complementary_outcomes, news, ...) ----------------


def _process_signal_strategy(
    name: str,
    strategy: SignalStrategy,
    market: MarketMetadata,
    book: OrderBook,
    latest_books: dict[str, OrderBook],
    order_manager: OrderManager,
    portfolio: Portfolio,
    predictions: list[Prediction],
) -> None:
    context_books = {
        token_id: latest_books[token_id] for token_id in market.token_ids if token_id in latest_books
    }
    signal = strategy.evaluate(market, book, SignalContext(order_books=context_books))
    if signal is None:
        return

    if signal.token_id is None:
        decisions = order_manager.handle_multi_leg_signal(signal, market)
    else:
        decisions = [order_manager.handle_signal(signal, market, book)]

    # Strategies that price in a taker fee when deciding whether to trade
    # (see SignalStrategy.fee_rate_for) should have that same fee actually
    # deducted from the portfolio, not just used as a firing threshold -
    # otherwise reported P&L is gross, not net, of the exact fee the signal
    # itself required to clear before it fired. Computed per-decision (not
    # once for the whole signal) since ComplementaryOutcomesSignal's real fee
    # is price-dependent - each leg can have a materially different rate.
    for decision in decisions:
        if decision.status not in (OrderStatus.SUBMITTED, OrderStatus.DRY_RUN) or decision.intent is None:
            continue
        intent = decision.intent
        fee_rate = strategy.fee_rate_for(intent.price, market)
        portfolio.apply_fill(
            token_id=intent.token_id,
            condition_id=intent.condition_id,
            side=intent.side,
            price=intent.price,
            size=intent.size,
            timestamp=book.received_at,
            strategy=name,
            fee_rate=fee_rate,
        )
        if signal.token_id is not None:
            # Multi-leg (arb) signals don't have a single fair-value price to
            # grade for calibration - only single-token signals do.
            fair_price = implied_fair_price(intent.price, signal.edge_estimate, signal.side)
            predictions.append(Prediction(token_id=intent.token_id, probability=fair_price))


# ---- Market making --------------------------------------------------------------------


def _process_market_making(
    name: str,
    strategy: MarketMakingStrategy,
    market: MarketMetadata,
    book: OrderBook,
    order_manager: OrderManager,
    portfolio: Portfolio,
    open_quotes: dict[str, QuotePair],
) -> None:
    previous_quote = open_quotes.get(book.token_id)
    if previous_quote is not None:
        _fill_crossed_quote(name, previous_quote, book, strategy, portfolio)

    quote_pair = strategy.quote(market, book)
    decisions = order_manager.handle_quote(quote_pair, market)
    open_quotes[book.token_id] = _approved_quote_pair(book.token_id, decisions, previous_quote)


def _approved_quote_pair(
    token_id: str, decisions, previous_quote: QuotePair | None
) -> QuotePair:
    """The risk-gate-approved quote actually resting in the book - not
    necessarily the strategy's raw request, for two reasons:

    1. A side may be resized or missing entirely (hard cap/no room).
    2. If the strategy re-quotes the *same* price/size as last tick (a static
       book), OrderManager's idempotency guard correctly dedupes it -
       REJECTED, but with `intent` populated (unlike a real risk rejection,
       where intent is None). That's not a cancellation, it's "this order is
       still resting unchanged," so that side carries forward from
       `previous_quote` rather than being wiped to None - otherwise every
       quote silently vanishes from tracking the moment the price goes flat
       for 60s, and can never register a fill again.
    """
    bid = ask = None
    for decision in decisions:
        if decision.intent is None:
            continue  # hard rejection (no room/edge) - nothing resting on this side
        side = decision.intent.side
        if decision.status in (OrderStatus.SUBMITTED, OrderStatus.DRY_RUN):
            quote = Quote(price=decision.intent.price, size=decision.intent.size)
        else:  # REJECTED with an intent attached == deduped duplicate, not a cancellation
            quote = (previous_quote.bid if side is Side.BUY else previous_quote.ask) if previous_quote else None
        if side is Side.BUY:
            bid = quote
        else:
            ask = quote
    return QuotePair(token_id=token_id, bid=bid, ask=ask)


def _fill_crossed_quote(
    name: str,
    quote_pair: QuotePair,
    new_book: OrderBook,
    strategy: MarketMakingStrategy,
    portfolio: Portfolio,
) -> None:
    """One-step-lookahead fill model for resting orders: a quote is treated
    as filled if the next snapshot's opposite-side price crosses through it."""
    best_bid = new_book.best_bid
    best_ask = new_book.best_ask

    if quote_pair.ask is not None and best_bid is not None and best_bid.price >= quote_pair.ask.price:
        portfolio.apply_fill(
            token_id=quote_pair.token_id,
            condition_id=new_book.condition_id,
            side=Side.SELL,
            price=quote_pair.ask.price,
            size=quote_pair.ask.size,
            timestamp=new_book.received_at,
            strategy=name,
        )
        strategy.record_fill(quote_pair.token_id, Side.SELL, quote_pair.ask.size)

    if quote_pair.bid is not None and best_ask is not None and best_ask.price <= quote_pair.bid.price:
        portfolio.apply_fill(
            token_id=quote_pair.token_id,
            condition_id=new_book.condition_id,
            side=Side.BUY,
            price=quote_pair.bid.price,
            size=quote_pair.bid.size,
            timestamp=new_book.received_at,
            strategy=name,
        )
        strategy.record_fill(quote_pair.token_id, Side.BUY, quote_pair.bid.size)
