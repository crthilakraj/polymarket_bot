"""Shared py-clob-client instance, built from config.settings.

Constructs a Level 2 (fully authenticated) ClobClient once and caches it, so
callers share one client instead of each building their own. Note (see
README): py-clob-client is archived upstream in favor of a newer SDK, but the
order-signing/placement surface used here is still what's documented as the
supported way to trade on the CLOB today.
"""

import logging

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

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
