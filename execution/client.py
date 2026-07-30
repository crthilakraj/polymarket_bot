"""Shared py-clob-client instance, built from config.settings.

Constructs a Level 2 (fully authenticated) ClobClient once and caches it, so
callers share one client instead of each building their own. Note (see
README): py-clob-client is archived upstream in favor of a newer SDK, but the
order-signing/placement surface used here is still what's documented as the
supported way to trade on the CLOB today.
"""

import logging

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, AssetType, BalanceAllowanceParams

from config import settings

logger = logging.getLogger(__name__)

POLYGON_CHAIN_ID = 137

_client: ClobClient | None = None


def get_client() -> ClobClient:
    """Return a configured, authenticated ClobClient instance (cached after
    the first call). Raises RuntimeError if trading credentials aren't set."""
    global _client
    if _client is not None:
        return _client

    settings.require_trading_credentials()
    client = ClobClient(
        settings.clob_api_url,
        chain_id=POLYGON_CHAIN_ID,
        key=settings.private_key,
        creds=ApiCreds(
            api_key=settings.clob_api_key,
            api_secret=settings.clob_api_secret,
            api_passphrase=settings.clob_api_passphrase,
        ),
        signature_type=settings.clob_signature_type,
        funder=settings.clob_funder_address,
    )
    logger.info("initialized ClobClient for %s", settings.clob_api_url)
    _client = client
    return client


def reset_client() -> None:
    """Clear the cached client. Mainly for tests, or after rotating credentials."""
    global _client
    _client = None


def get_collateral_balance_usd(client: ClobClient) -> float:
    """Live, on-chain-backed USDC collateral balance (in dollars) for the
    authenticated wallet, via Polymarket's /balance-allowance endpoint.
    Requires Level 2 auth (the client returned by get_client() already has
    this). Used by OrderManager to cap live order sizing against actual
    available funds, not just the configured LIVE_MAX_FUND_USD ceiling."""
    response = client.get_balance_allowance(
        BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    )
    raw_balance = response.get("balance") if isinstance(response, dict) else None
    if raw_balance is None:
        raise RuntimeError(f"unexpected balance-allowance response shape: {response!r}")
    # USDC has 6 decimals on Polygon; the API reports balance as a raw
    # integer string in that base unit, not dollars.
    return int(raw_balance) / 1_000_000
