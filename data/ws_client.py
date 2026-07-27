"""CLOB WebSocket client: subscribes to the public `market` channel for a
configurable list of CLOB token ids and normalizes book/price_change events
into OrderBook snapshots.

Protocol (Polymarket CLOB WS market channel, no auth required):
- Connect: wss://ws-subscriptions-clob.polymarket.com/ws/market
- Subscribe: {"type": "market", "assets_ids": [...token_ids], "custom_feature_enabled": true}
- Heartbeat: send the text "PING" every 10s; server replies with the text "PONG".
  The connection is dropped if PING isn't sent, so this uses ping_interval=None
  on the transport and manages heartbeats itself.
- Events (`event_type`): "book" is a full snapshot (bids/asks/timestamp/hash);
  "price_change" is a delta where a level with size "0" has been removed.
Subscriptions are by *token id* (one per outcome), not condition_id - resolve
condition_ids to token_ids via data.gamma_client first.
"""

import asyncio
import contextlib
import json
import logging
import random
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timezone

import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from config import settings
from data.models import OrderBook, PriceLevel

logger = logging.getLogger(__name__)

PING_INTERVAL_SECONDS = 10
MAX_RECONNECT_BACKOFF_SECONDS = 60

OnBookUpdate = Callable[[OrderBook], "None | Awaitable[None]"]


class _BookState:
    """Mutable per-token book state, used to apply `price_change` deltas onto
    the most recent `book` snapshot."""

    __slots__ = ("bids", "asks", "timestamp", "book_hash")

    def __init__(self) -> None:
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.timestamp: datetime | None = None
        self.book_hash: str | None = None

    def to_order_book(self, token_id: str) -> OrderBook:
        return OrderBook(
            token_id=token_id,
            condition_id=None,
            bids=[PriceLevel(p, s) for p, s in sorted(self.bids.items(), reverse=True)],
            asks=[PriceLevel(p, s) for p, s in sorted(self.asks.items())],
            exchange_timestamp=self.timestamp,
            book_hash=self.book_hash,
        )


def _parse_timestamp(raw: str | int | None) -> datetime | None:
    if raw is None:
        return None
    return datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc)


class ClobWebSocketClient:
    """Maintains a resilient connection to the CLOB market WS channel."""

    def __init__(
        self,
        token_ids: Sequence[str],
        on_book_update: OnBookUpdate,
        url: str | None = None,
        ping_interval: float = PING_INTERVAL_SECONDS,
        max_backoff: float = MAX_RECONNECT_BACKOFF_SECONDS,
    ) -> None:
        if not token_ids:
            raise ValueError("token_ids must be non-empty")
        self._token_ids = list(token_ids)
        self._on_book_update = on_book_update
        self._url = url or settings.clob_ws_market_url
        self._ping_interval = ping_interval
        self._max_backoff = max_backoff
        self._books: dict[str, _BookState] = {}
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    async def run(self) -> None:
        """Connect and process messages until stop() is called, reconnecting
        with exponential backoff (+ jitter) on any connection failure."""
        backoff = 1.0
        while not self._stopped:
            try:
                await self._connect_and_listen()
                backoff = 1.0  # server closed cleanly - don't punish the next attempt
            except (ConnectionClosed, OSError) as exc:
                logger.warning("CLOB WS connection error: %s", exc)
            except Exception:
                logger.exception("unexpected error in CLOB WS client")

            if self._stopped:
                return

            sleep_for = backoff + random.uniform(0, backoff * 0.1)
            logger.info("reconnecting to CLOB WS in %.1fs", sleep_for)
            await asyncio.sleep(sleep_for)
            backoff = min(backoff * 2, self._max_backoff)

    async def _connect_and_listen(self) -> None:
        logger.info("connecting to %s for %d token(s)", self._url, len(self._token_ids))
        async with websockets.connect(self._url, ping_interval=None) as ws:
            await ws.send(
                json.dumps(
                    {
                        "type": "market",
                        "assets_ids": self._token_ids,
                        "custom_feature_enabled": True,
                    }
                )
            )
            ping_task = asyncio.create_task(self._ping_loop(ws))
            try:
                async for raw_message in ws:
                    await self._handle_message(raw_message)
            finally:
                ping_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await ping_task

    async def _ping_loop(self, ws: ClientConnection) -> None:
        while True:
            await asyncio.sleep(self._ping_interval)
            await ws.send("PING")

    async def _handle_message(self, raw_message: str | bytes) -> None:
        if raw_message in ("PONG", b"PONG"):
            return
        try:
            payload = json.loads(raw_message)
        except json.JSONDecodeError:
            logger.warning("received non-JSON WS message: %r", raw_message)
            return

        events = payload if isinstance(payload, list) else [payload]
        for event in events:
            await self._handle_event(event)

    async def _handle_event(self, event: dict) -> None:
        event_type = event.get("event_type")
        if event_type == "book":
            book = self._apply_snapshot(event)
        elif event_type == "price_change":
            book = self._apply_delta(event)
        elif event_type == "tick_size_change":
            logger.info(
                "tick size change for %s: %s -> %s",
                event.get("asset_id"),
                event.get("old_tick_size"),
                event.get("new_tick_size"),
            )
            return
        else:
            return

        if book is None:
            return

        result = self._on_book_update(book)
        if asyncio.iscoroutine(result):
            await result

    def _apply_snapshot(self, event: dict) -> OrderBook | None:
        token_id = event.get("asset_id")
        if token_id is None:
            return None
        state = self._books.setdefault(token_id, _BookState())
        state.bids = {
            float(level["price"]): float(level["size"]) for level in event.get("bids", [])
        }
        state.asks = {
            float(level["price"]): float(level["size"]) for level in event.get("asks", [])
        }
        state.timestamp = _parse_timestamp(event.get("timestamp"))
        state.book_hash = event.get("hash")
        return state.to_order_book(token_id)

    def _apply_delta(self, event: dict) -> OrderBook | None:
        book: OrderBook | None = None
        for change in event.get("price_changes", []):
            token_id = change.get("asset_id")
            state = self._books.get(token_id) if token_id else None
            if state is None:
                # No snapshot yet for this token - ignore the delta until `book` arrives.
                continue
            price = float(change["price"])
            size = float(change["size"])
            side = state.bids if change.get("side") == "BUY" else state.asks
            if size == 0:
                side.pop(price, None)
            else:
                side[price] = size
            state.timestamp = _parse_timestamp(event.get("timestamp")) or state.timestamp
            state.book_hash = change.get("hash", state.book_hash)
            book = state.to_order_book(token_id)
        return book
