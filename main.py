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
    backtest/engine.py. Wire one up and add it to this dict if you need it live.

    market_making is off by default (ENABLE_MARKET_MAKING=true to turn it
    back on): a live dry-run check found no proven edge (P&L inconclusive/
    slightly negative on a small sample - see scripts/report_market_making_pnl.py),
    while it was responsible for 9.5M of 9.55M rows (99.5%) in decisions_log
    over one ~48h run - almost entirely rejected orders once the portfolio's
    exposure cap filled up - which is what made scripts/cli.py's journal
    replay time out and need bounding. Not worth the operational cost without
    a demonstrated edge."""
    strategies: dict[str, SignalStrategy | MarketMakingStrategy] = {
        "complementary_outcomes": ComplementaryOutcomesSignal(min_edge_bps=settings.min_edge_bps),
    }
    if settings.enable_market_making:
        strategies["market_making"] = MarketMakingStrategy(
            position_limits=PositionLimits.from_settings(settings)
        )
    return strategies


def build_order_manager() -> OrderManager:
    """dry_run mode (default) needs nothing further and sizes against
    MAX_PORTFOLIO_EXPOSURE_USD (the paper-trading cap). Live trading requires
    both DRY_RUN=false and LIVE_TRADING_CONFIRMED=true (see config.py),
    sizes against the separate LIVE_MAX_FUND_USD cap instead, and further
    clamps that cap to the real, live USDC balance queried from Polymarket
    on every risk check (see OrderManager._effective_risk_limits) - so a
    live order can never be sized against funds the wallet doesn't actually
    have, only against a static .env number.

    Also refuses to start live trading if either of two independent
    restriction checks trips, both found live 2026-07-31 after this box (AWS
    eu-west-2 / London, UK - on Polymarket's close-only list) got blocked
    mid-session on a live canary test:
      1. check_geoblock() - Polymarket's public, unauthenticated, real-time
         IP-based check. This is the one that actually matters: it directly
         answers "will Polymarket's edge reject orders from this box right
         now", with no order attempt needed.
      2. client.get_closed_only_mode() - an account-level compliance-ban
         flag. Kept as a secondary check since it covers a different failure
         mode (account sanctioned regardless of IP), but on its own it is
         NOT sufficient - it returned closed_only=False for this exact
         account the whole time real orders were being rejected by check #1.
    Even with both, a geoblock could in principle start applying mid-session
    (this check only runs at startup) - see execution.orders.GeoRestrictedError
    and OrderManager._geo_restricted for the runtime circuit breaker that
    catches that case (stops after the first live rejection instead of
    repeating the same doomed request, as happened live before this existed:
    72 failed orders before a human noticed)."""
    settings.require_live_trading_confirmation()

    client = None
    live_balance_fn = None
    bankroll_cap = settings.max_portfolio_exposure_usd
    if not settings.dry_run:
        from execution.client import check_geoblock, get_client, get_collateral_balance_usd

        geoblock_status = check_geoblock()
        if geoblock_status.get("blocked"):
            if not settings.override_geoblock_check:
                raise SystemExit(
                    f"Polymarket is blocking trading from this network location "
                    f"(ip={geoblock_status.get('ip')}, country={geoblock_status.get('country')}, "
                    f"region={geoblock_status.get('region')}). Refusing to start live trading. "
                    "Set OVERRIDE_GEOBLOCK_CHECK=true in .env to proceed anyway (only do this "
                    "if you've confirmed via a real order that this account/IP combination can "
                    "actually trade despite this endpoint's report - see canary test 2026-08-02: "
                    "an eu-west-1/Ireland account placed and filled a real order here while this "
                    "check reported blocked=true). "
                    "See https://docs.polymarket.com/developers/CLOB/geoblock"
                )
            logger.warning(
                "check_geoblock() reports blocked=true (ip=%s, country=%s, region=%s) but "
                "OVERRIDE_GEOBLOCK_CHECK=true - proceeding anyway. The runtime GeoRestrictedError "
                "circuit breaker in OrderManager is still active as a second layer.",
                geoblock_status.get("ip"),
                geoblock_status.get("country"),
                geoblock_status.get("region"),
            )

        client = get_client()
        ban_status = client.get_closed_only_mode()
        if isinstance(ban_status, dict) and ban_status.get("closed_only"):
            raise SystemExit(
                "Polymarket reports this account is in closed-only mode (compliance ban) - "
                "new positions cannot be opened. Refusing to start live trading. "
                "See https://docs.polymarket.com/developers/CLOB/geoblock"
            )
        live_balance_fn = lambda: get_collateral_balance_usd(client)  # noqa: E731
        bankroll_cap = settings.live_max_fund_usd
        logger.warning("LIVE TRADING IS ENABLED - real orders will be placed on Polymarket")

    return OrderManager(
        risk_limits=RiskLimits.from_settings(settings, portfolio_exposure_usd_override=bankroll_cap),
        client=client,
        dry_run=settings.dry_run,
        live_balance_fn=live_balance_fn,
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
    pending_writes: set[asyncio.Task] = set()

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

        def _on_write_done(task: asyncio.Task) -> None:
            pending_writes.discard(task)
            exc = task.exception() if not task.cancelled() else None
            if exc is not None:
                logger.warning("order book DB write failed (dropped, not retried): %s", exc)

        async def on_book_update(book: OrderBook) -> None:
            # Trade decisions run on the fresh in-memory book immediately;
            # persisting a snapshot to disk is disposable, replay-only data
            # (scripts/checkpoint_and_prune.py already prunes it once a
            # checkpoint no longer needs it) and must never gate or delay a
            # decision - especially since write contention with
            # checkpoint_and_prune.py/refresh_all_metadata.py has twice
            # caused real "database is locked" failures live (see
            # HANDOVER.md). Fire-and-forget via asyncio.create_task instead
            # of awaiting it: a lost snapshot only costs some backtest-replay
            # fidelity, never a trading decision. DataStore.save_order_book
            # is already thread-safe on its own (has its own internal lock).
            market = token_to_market.get(book.token_id)
            book.condition_id = market.condition_id if market else book.condition_id
            latest_books[book.token_id] = book

            if market is not None:
                for name, strategy in strategies.items():
                    if isinstance(strategy, MarketMakingStrategy):
                        _run_market_making(name, strategy, market, book, order_manager, journal)
                    else:
                        _run_signal_strategy(
                            name, strategy, market, book, latest_books, order_manager, journal
                        )

            write_task = asyncio.create_task(asyncio.to_thread(store.save_order_book, book))
            pending_writes.add(write_task)
            write_task.add_done_callback(_on_write_done)

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
        if pending_writes:
            await asyncio.gather(*pending_writes, return_exceptions=True)
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
