"""Order placement and cancellation via py-clob-client, with retry on
transient failures.

Retries only re-POST the *same already-signed* order - they never re-sign.
py-clob-client embeds a fresh random salt in every order it signs, so
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

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.exceptions import PolyApiException

from execution.models import OrderIntent

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
INITIAL_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 10.0


class OrderPlacementError(RuntimeError):
    """Raised when placing an order fails (non-retryable, or retries exhausted)."""


def _is_retryable(exc: PolyApiException) -> bool:
    # status_code is None for a network-level failure (no response at all);
    # 429/5xx are worth retrying, other 4xx (bad request, auth, etc.) are not.
    return exc.status_code is None or exc.status_code == 429 or exc.status_code >= 500


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
    """Cancel an open order."""
    return client.cancel(order_id)
