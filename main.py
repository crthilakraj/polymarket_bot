"""Runs the full trading pipeline live: the data layer feeds strategies in
signals/, strategy output routes through execution/'s risk gate, and every
signal/quote and the decision it produced is logged - nothing is ever
submitted to the exchange unless you've explicitly opted into live trading
(see config.Settings.require_live_trading_confirmation()). Runs in dry-run
mode by default.

Usage:
    uv run python main.py

Requires MARKET_CONDITION_IDS in .env. While this runs (or after stopping
it), check what the bot has been doing with scripts/cli.py (positions /
signals / pnl) - both read from the same persisted journal and market data
this process writes to DB_PATH.
"""

import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import settings
from data.gamma_client import GammaClient
from data.models import OrderBook
from data.store import DataStore
from data.ws_client import ClobWebSocketClient
from execution.journal import DecisionJournal
from execution.models import OrderStatus
from execution.order_manager import OrderManager
from execution.risk import RiskLimits
from logging_config import configure_logging
from signals.base import SignalContext, SignalStrategy
from signals.complementary_outcomes import ComplementaryOutcomesSignal
from signals.market_making.models import PositionLimits
from signals.market_making.strategy import MarketMakingStrategy

logger = logging.getLogger(__name__)


def build_strategies() -> dict[str, SignalStrategy | MarketMakingStrategy]:
    """The strategies the live pipeline runs by default. signals/news isn't
    included here: NewsEdgeSignal only fires when a headline is attached via
    SignalContext.metadata["headline"], and this pipeline has no live
    headline feed wired in - the same documented limitation as
    backtest/engine.py. Wire one up and add it to this dict if you need it live."""
    return {
        "complementary_outcomes": ComplementaryOutcomesSignal(),
        "market_making": MarketMakingStrategy(position_limits=PositionLimits.from_settings(settings)),
    }


def build_order_manager() -> OrderManager:
    """dry_run mode (default) needs nothing further. Live trading requires
    both DRY_RUN=false and LIVE_TRADING_CONFIRMED=true - see config.py."""
    settings.require_live_trading_confirmation()

    client = None
    if not settings.dry_run:
        from execution.client import get_client

        client = get_client()
        logger.warning("LIVE TRADING IS ENABLED - real orders will be placed on Polymarket")

    return OrderManager(
        risk_limits=RiskLimits.from_settings(settings),
        client=client,
        dry_run=settings.dry_run,
    )


async def main() -> None:
    configure_logging()

    if not settings.market_condition_ids:
        raise SystemExit(
            "MARKET_CONDITION_IDS is not set. Add a comma-separated list of condition_ids "
            "to .env, e.g. MARKET_CONDITION_IDS=0xabc...,0xdef..."
        )

    order_manager = build_order_manager()
    strategies = build_strategies()
    store = DataStore(settings.db_path)
    journal = DecisionJournal(settings.db_path)
    gamma = GammaClient()

    try:
        markets = await asyncio.to_thread(
            gamma.get_markets_by_condition_ids, settings.market_condition_ids
        )
        if not markets:
            raise SystemExit(
                f"Gamma returned no markets for condition_ids={settings.market_condition_ids}"
            )
        for market in markets:
            store.save_market_metadata(market)
            logger.info(
                "tracking market %s: %r (%d outcomes)",
                market.condition_id,
                market.question,
                len(market.token_ids),
            )

        token_to_market = {token_id: market for market in markets for token_id in market.token_ids}
        latest_books: dict[str, OrderBook] = {}

        def on_book_update(book: OrderBook) -> None:
            market = token_to_market.get(book.token_id)
            book.condition_id = market.condition_id if market else book.condition_id
            store.save_order_book(book)
            latest_books[book.token_id] = book

            if market is None:
                return
            for name, strategy in strategies.items():
                if isinstance(strategy, MarketMakingStrategy):
                    _run_market_making(name, strategy, market, book, order_manager, journal)
                else:
                    _run_signal_strategy(
                        name, strategy, market, book, latest_books, order_manager, journal
                    )

        ws_client = ClobWebSocketClient(token_ids=list(token_to_market), on_book_update=on_book_update)

        async def refresh_metadata_loop() -> None:
            while True:
                await asyncio.sleep(settings.gamma_refresh_interval_seconds)
                try:
                    refreshed = await asyncio.to_thread(
                        gamma.get_markets_by_condition_ids, settings.market_condition_ids
                    )
                    for market in refreshed:
                        store.save_market_metadata(market)
                    logger.info("refreshed metadata for %d market(s)", len(refreshed))
                except Exception:
                    logger.exception("gamma metadata refresh failed, will retry next interval")

        logger.info(
            "starting live pipeline: %d market(s), dry_run=%s, strategies=%s",
            len(markets),
            settings.dry_run,
            list(strategies),
        )
        await asyncio.gather(ws_client.run(), refresh_metadata_loop())
    finally:
        gamma.close()
        journal.close()
        store.close()


def _run_signal_strategy(
    name: str,
    strategy: SignalStrategy,
    market,
    book: OrderBook,
    latest_books: dict[str, OrderBook],
    order_manager: OrderManager,
    journal: DecisionJournal,
) -> None:
    context_books = {
        token_id: latest_books[token_id] for token_id in market.token_ids if token_id in latest_books
    }
    signal = strategy.evaluate(market, book, SignalContext(order_books=context_books))
    if signal is None:
        return

    now = datetime.now(timezone.utc)
    journal.record_signal(strategy=name, condition_id=market.condition_id, signal=signal, timestamp=now)

    if signal.token_id is None:
        decisions = order_manager.handle_multi_leg_signal(signal, market)
    else:
        decisions = [order_manager.handle_signal(signal, market, book)]

    for decision in decisions:
        journal.record_decision(
            strategy=name, condition_id=market.condition_id, decision=decision, timestamp=now
        )
        _log_decision(name, decision)


def _run_market_making(
    name: str,
    strategy: MarketMakingStrategy,
    market,
    book: OrderBook,
    order_manager: OrderManager,
    journal: DecisionJournal,
) -> None:
    now = datetime.now(timezone.utc)
    quote_pair = strategy.quote(market, book, now=now)
    journal.record_quote(
        strategy=name, condition_id=market.condition_id, quote_pair=quote_pair, timestamp=now
    )

    decisions = order_manager.handle_quote(quote_pair, market)
    for decision in decisions:
        journal.record_decision(
            strategy=name, condition_id=market.condition_id, decision=decision, timestamp=now
        )
        _log_decision(name, decision)


def _log_decision(strategy: str, decision) -> None:
    if decision.status is OrderStatus.REJECTED:
        logger.debug("[%s] rejected: %s", strategy, "; ".join(decision.reasons) or "no reason given")
        return
    intent = decision.intent
    logger.info(
        "[%s] %s %s %.4f shares @ %.4f on token=%s (%s)",
        strategy,
        decision.status.value,
        intent.side.value,
        intent.size,
        intent.price,
        intent.token_id,
        "; ".join(decision.reasons) or "no resize",
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("stopped by user")
    except RuntimeError as exc:
        # Config-time guard failures (missing credentials, live trading not
        # explicitly confirmed) - a clean exit, not a stack trace.
        raise SystemExit(str(exc)) from None
