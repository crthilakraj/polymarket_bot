"""Order placement and cancellation via py-clob-client-v2, with retry on
transient failures.

Retries only re-POST the *same already-signed* order - they never re-sign.
py-clob-client-v2 embeds a fresh random salt in every order it signs, so
resubmitting a freshly re-signed "retry" would be a distinct valid order; if
the original attempt actually reached the exchange despite a client-side
error (timeout, connection drop), both could end up live. Signing once and
retrying only the POST keeps a retry idempotent at the exchange.

The other half of idempotency - not re-deciding-and-resubmitting the same
logical trade twice - is OrderManager's job (it dedupes before this module is
even called); this module only guards the network layer.
"""

import logging
import time

from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import OrderArgs, OrderPayload, OrderType
from py_clob_client_v2.exceptions import PolyApiException

from execution.models import OrderIntent

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
INITIAL_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 10.0


class OrderPlacementError(RuntimeError):
    """Raised when placing an order fails (non-retryable, or retries exhausted)."""


class GeoRestrictedError(OrderPlacementError):
    """Raised when Polymarket rejects an order because the request's IP is in
    a jurisdiction on their restricted list (see
    https://docs.polymarket.com/developers/CLOB/geoblock). Found live
    2026-07-31: this box runs in AWS eu-west-2 (London), and the UK is on
    Polymarket's "close-only" list - new positions are blocked on every
    single request, not intermittently.

    main.py's build_order_manager() now checks this ahead of time via
    execution.client.check_geoblock() (Polymarket's public, real-time,
    IP-based check) before ever starting live trading - that's the primary
    defense. This exception (and OrderManager's circuit breaker below) exist
    as a second layer for the case that check can't cover: a block starting
    mid-session, after startup already checked clean. Note
    client.get_closed_only_mode() is NOT a substitute for check_geoblock() -
    it reflects an account-level compliance flag, not this live per-request
    IP check, and returned closed_only=False for this exact account the
    whole time every order was being rejected with this error. A distinct
    exception type exists so callers can treat it as structural and
    permanent for this network location, not a transient failure worth
    retrying - see
    OrderManager._geo_restricted in order_manager.py, which stops attempting
    further live orders entirely once this fires once, rather than repeating
    the same doomed request (found live: 72 failed orders before a human
    caught it, in the incident that motivated this)."""


def _is_retryable(exc: PolyApiException) -> bool:
    # status_code is None for a network-level failure (no response at all);
    # 429/5xx are worth retrying, other 4xx (bad request, auth, etc.) are not.
    return exc.status_code is None or exc.status_code == 429 or exc.status_code >= 500


def _is_geo_restricted(exc: PolyApiException) -> bool:
    if exc.status_code != 403:
        return False
    error_msg = exc.error_msg
    text = error_msg.get("error", "") if isinstance(error_msg, dict) else str(error_msg)
    return "geoblock" in text.lower() or "restricted in your region" in text.lower()


def place_order(client: ClobClient, intent: OrderIntent) -> dict:
    """Sign intent once, then POST it with retry on transient failures.
    Raises OrderPlacementError on a non-retryable failure, an exchange-level
    rejection, or if retries are exhausted."""
    order_args = OrderArgs(
        token_id=intent.token_id,
        price=intent.price,
        size=intent.size,
        side=intent.side.value,
    )
    signed_order = client.create_order(order_args)

    backoff = INITIAL_BACKOFF_SECONDS
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.post_order(signed_order, OrderType.GTC)
        except PolyApiException as exc:
            last_exc = exc
            if _is_geo_restricted(exc):
                raise GeoRestrictedError(
                    f"order placement failed for {intent.idempotency_key}: {exc}"
                ) from exc
            if not _is_retryable(exc):
                raise OrderPlacementError(
                    f"order placement failed for {intent.idempotency_key}: {exc}"
                ) from exc
            logger.warning(
                "order post failed (attempt %d/%d) for %s: %s",
                attempt,
                MAX_RETRIES,
                intent.idempotency_key,
                exc,
            )
            if attempt < MAX_RETRIES:
                time.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
            continue

        if isinstance(response, dict) and response.get("success") is False:
            # Exchange processed the request but rejected the order (bad price,
            # insufficient balance, ...) - retrying the same order won't help.
            raise OrderPlacementError(
                f"order rejected by exchange for {intent.idempotency_key}: "
                f"{response.get('errorMsg', response)}"
            )
        return response

    raise OrderPlacementError(
        f"order placement failed after {MAX_RETRIES} attempts for {intent.idempotency_key}: {last_exc}"
    ) from last_exc


def cancel_order(client: ClobClient, order_id: str) -> dict:
    """Cancel an open order. v2's client.cancel_order() takes an OrderPayload,
    not a bare order_id string like v1's client.cancel() did."""
    return client.cancel_order(OrderPayload(orderID=order_id))
