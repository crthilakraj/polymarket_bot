"""Shared py-clob-client-v2 instance, built from config.settings.

Constructs a Level 2 (fully authenticated) ClobClient once and caches it, so
callers share one client instead of each building their own.

Uses py-clob-client-v2, not the original py-clob-client: Polymarket migrated
the CLOB to V2 on 2026-04-28 (new order struct, EIP-712 exchange domain
bumped 1->2) and archived the original client on 2026-05-11 - every order
signed by the old client is rejected with "invalid order version, please use
the latest clob-client". Found live on 2026-07-31 during a canary test (72/72
orders failed, $0 lost since orders were rejected before any funds moved).
See https://docs.polymarket.com/v2-migration.
"""

import logging

import httpx
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import ApiCreds, AssetType, BalanceAllowanceParams

from config import settings

logger = logging.getLogger(__name__)

POLYGON_CHAIN_ID = 137
GEOBLOCK_URL = "https://polymarket.com/api/geoblock"

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
    """Live, on-chain-backed collateral balance (in dollars) for the
    authenticated wallet, via Polymarket's /balance-allowance endpoint.
    Requires Level 2 auth (the client returned by get_client() already has
    this). Used by OrderManager to cap live order sizing against actual
    available funds, not just the configured LIVE_MAX_FUND_USD ceiling.

    Post-V2, collateral is pUSD rather than USDC.e - still 6 decimals (per
    https://docs.polymarket.com/concepts/pusd), so the conversion below is
    unchanged, but a wallet holding un-wrapped USDC.e won't show up here
    until it's wrapped via the Collateral Onramp's wrap() function."""
    response = client.get_balance_allowance(
        BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    )
    raw_balance = response.get("balance") if isinstance(response, dict) else None
    if raw_balance is None:
        raise RuntimeError(f"unexpected balance-allowance response shape: {response!r}")
    return int(raw_balance) / 1_000_000


def check_geoblock() -> dict:
    """Real-time IP-based trading-restriction check, via Polymarket's public
    (unauthenticated) /api/geoblock endpoint - {"blocked": bool, "ip": str,
    "country": str, "region": str}.

    This is the check that actually matters for live trading, found live
    2026-07-31 AFTER get_collateral_balance_usd()'s /balance-allowance and
    the CLOB API's client.get_closed_only_mode() both failed to predict a
    region-based order rejection: those two reflect account-level state
    (wallet balance, compliance bans), not the live, per-request IP
    geolocation check that Polymarket's edge actually enforces against
    trading. This endpoint queries that same live IP-based check directly,
    with no order attempt needed - confirmed live to correctly report
    blocked=true for a box in AWS eu-west-2 (London, UK - on Polymarket's
    close-only list, see https://docs.polymarket.com/developers/CLOB/geoblock)
    while the account-level checks both said everything was fine.

    Called from main.py's build_order_manager() before ever starting live
    trading. Fails safe on a network error: raises rather than assuming
    unblocked, since the whole point is to refuse to trade blind."""
    response = httpx.get(GEOBLOCK_URL, timeout=10.0)
    response.raise_for_status()
    return response.json()
