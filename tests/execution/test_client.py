import pytest

import execution.client as client_module


class _FakeSettings:
    clob_api_url = "https://clob.example.com"
    private_key = None
    clob_api_key = None
    clob_api_secret = None
    clob_api_passphrase = None
    clob_signature_type = 0
    clob_funder_address = None

    def require_trading_credentials(self) -> None:
        missing = [
            name
            for name, value in (
                ("POLYMARKET_PRIVATE_KEY", self.private_key),
                ("CLOB_API_KEY", self.clob_api_key),
                ("CLOB_API_SECRET", self.clob_api_secret),
                ("CLOB_API_PASSPHRASE", self.clob_api_passphrase),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")


@pytest.fixture(autouse=True)
def _reset_cached_client():
    client_module.reset_client()
    yield
    client_module.reset_client()


def test_get_client_raises_when_credentials_missing(monkeypatch):
    monkeypatch.setattr(client_module, "settings", _FakeSettings())

    with pytest.raises(RuntimeError):
        client_module.get_client()


def test_get_client_constructs_and_caches_a_single_instance(monkeypatch):
    settings = _FakeSettings()
    settings.private_key = "0x" + "11" * 32
    settings.clob_api_key = "key"
    settings.clob_api_secret = "secret"
    settings.clob_api_passphrase = "pass"
    monkeypatch.setattr(client_module, "settings", settings)

    first = client_module.get_client()
    second = client_module.get_client()

    assert first is second
    assert first.get_address() is not None


def test_reset_client_forces_reconstruction(monkeypatch):
    settings = _FakeSettings()
    settings.private_key = "0x" + "11" * 32
    settings.clob_api_key = "key"
    settings.clob_api_secret = "secret"
    settings.clob_api_passphrase = "pass"
    monkeypatch.setattr(client_module, "settings", settings)

    first = client_module.get_client()
    client_module.reset_client()
    second = client_module.get_client()

    assert first is not second


class _FakeClobClient:
    def __init__(self, response):
        self._response = response

    def get_balance_allowance(self, params=None):
        return self._response


def test_get_collateral_balance_usd_converts_from_usdc_base_units():
    # USDC has 6 decimals - the API reports the raw integer string, not dollars.
    fake_client = _FakeClobClient({"balance": "123456789", "allowances": {}})

    balance = client_module.get_collateral_balance_usd(fake_client)

    assert balance == pytest.approx(123.456789)


def test_get_collateral_balance_usd_raises_on_unexpected_response_shape():
    fake_client = _FakeClobClient({"unexpected": "shape"})

    with pytest.raises(RuntimeError):
        client_module.get_collateral_balance_usd(fake_client)
