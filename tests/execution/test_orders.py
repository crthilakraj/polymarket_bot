import pytest
from py_clob_client_v2.exceptions import PolyApiException

from execution.models import OrderIntent
from execution.orders import GeoRestrictedError, OrderPlacementError, cancel_order, place_order
from signals.base import Side


class _FakeHttpResponse:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {"error": "boom"}

    def json(self):
        return self._body

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

    def cancel_order(self, payload):
        return {"canceled": payload.orderID}


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


GEOBLOCK_BODY = {
    "error": "Trading restricted in your region, please refer to available "
    "regions - https://docs.polymarket.com/developers/CLOB/geoblock"
}


def test_place_order_raises_geo_restricted_error_on_region_block(monkeypatch):
    monkeypatch.setattr("execution.orders.time.sleep", lambda _: None)
    client = FakeClobClient([PolyApiException(resp=_FakeHttpResponse(403, GEOBLOCK_BODY))])

    with pytest.raises(GeoRestrictedError):
        place_order(client, make_intent())

    assert client.post_order_calls == 1  # not retried - this will fail every time


def test_geo_restricted_error_is_a_subclass_of_order_placement_error():
    # So existing `except OrderPlacementError` call sites still catch it,
    # even ones that don't know about the more specific geoblock case.
    assert issubclass(GeoRestrictedError, OrderPlacementError)


def test_place_order_does_not_treat_ordinary_403_as_geo_restricted(monkeypatch):
    monkeypatch.setattr("execution.orders.time.sleep", lambda _: None)
    client = FakeClobClient([PolyApiException(resp=_FakeHttpResponse(403, {"error": "bad request"}))])

    with pytest.raises(OrderPlacementError) as exc_info:
        place_order(client, make_intent())

    assert not isinstance(exc_info.value, GeoRestrictedError)
