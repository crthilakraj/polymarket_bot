import pytest
from py_clob_client.exceptions import PolyApiException

from execution.models import OrderIntent
from execution.orders import OrderPlacementError, cancel_order, place_order
from signals.base import Side


class _FakeHttpResponse:
    def __init__(self, status_code):
        self.status_code = status_code

    def json(self):
        return {"error": "boom"}

    @property
    def text(self):
        return "boom"


def make_intent(**overrides) -> OrderIntent:
    defaults = dict(
        token_id="t1",
        condition_id="0xcond",
        side=Side.BUY,
        price=0.45,
        size=10.0,
        strategy="signal",
        idempotency_key="abc123",
    )
    defaults.update(overrides)
    return OrderIntent(**defaults)


class FakeClobClient:
    """post_order_results: a value (returned) or Exception (raised) per call, consumed in order."""

    def __init__(self, post_order_results):
        self._results = list(post_order_results)
        self.create_order_calls = 0
        self.post_order_calls = 0

    def create_order(self, order_args):
        self.create_order_calls += 1
        return {
            "signed": True,
            "token_id": order_args.token_id,
            "price": order_args.price,
            "size": order_args.size,
            "side": order_args.side,
        }

    def post_order(self, order, order_type):
        self.post_order_calls += 1
        result = self._results[self.post_order_calls - 1]
        if isinstance(result, Exception):
            raise result
        return result

    def cancel(self, order_id):
        return {"canceled": order_id}


def test_place_order_succeeds_on_first_try(monkeypatch):
    monkeypatch.setattr("execution.orders.time.sleep", lambda _: None)
    client = FakeClobClient([{"success": True, "orderID": "o1"}])

    response = place_order(client, make_intent())

    assert response["orderID"] == "o1"
    assert client.create_order_calls == 1
    assert client.post_order_calls == 1


def test_place_order_retries_on_transient_network_error_and_signs_only_once(monkeypatch):
    monkeypatch.setattr("execution.orders.time.sleep", lambda _: None)
    client = FakeClobClient(
        [PolyApiException(error_msg="connection reset"), {"success": True, "orderID": "o1"}]
    )

    response = place_order(client, make_intent())

    assert response["orderID"] == "o1"
    assert client.create_order_calls == 1  # signed exactly once despite the retry
    assert client.post_order_calls == 2


def test_place_order_retries_on_5xx(monkeypatch):
    monkeypatch.setattr("execution.orders.time.sleep", lambda _: None)
    client = FakeClobClient(
        [PolyApiException(resp=_FakeHttpResponse(503)), {"success": True, "orderID": "o1"}]
    )

    response = place_order(client, make_intent())

    assert response["orderID"] == "o1"
    assert client.post_order_calls == 2


def test_place_order_retries_on_429(monkeypatch):
    monkeypatch.setattr("execution.orders.time.sleep", lambda _: None)
    client = FakeClobClient(
        [PolyApiException(resp=_FakeHttpResponse(429)), {"success": True, "orderID": "o1"}]
    )

    response = place_order(client, make_intent())

    assert response["orderID"] == "o1"


def test_place_order_does_not_retry_on_4xx_client_error(monkeypatch):
    monkeypatch.setattr("execution.orders.time.sleep", lambda _: None)
    client = FakeClobClient([PolyApiException(resp=_FakeHttpResponse(400))])

    with pytest.raises(OrderPlacementError):
        place_order(client, make_intent())

    assert client.post_order_calls == 1


def test_place_order_raises_after_exhausting_retries(monkeypatch):
    monkeypatch.setattr("execution.orders.time.sleep", lambda _: None)
    client = FakeClobClient(
        [
            PolyApiException(error_msg="boom1"),
            PolyApiException(error_msg="boom2"),
            PolyApiException(error_msg="boom3"),
        ]
    )

    with pytest.raises(OrderPlacementError):
        place_order(client, make_intent())

    assert client.post_order_calls == 3
    assert client.create_order_calls == 1


def test_place_order_raises_on_exchange_level_rejection(monkeypatch):
    monkeypatch.setattr("execution.orders.time.sleep", lambda _: None)
    client = FakeClobClient([{"success": False, "errorMsg": "insufficient balance"}])

    with pytest.raises(OrderPlacementError):
        place_order(client, make_intent())

    assert client.post_order_calls == 1  # exchange-level rejection is not retried


def test_cancel_order_delegates_to_client():
    client = FakeClobClient([])
    assert cancel_order(client, "order-123") == {"canceled": "order-123"}
