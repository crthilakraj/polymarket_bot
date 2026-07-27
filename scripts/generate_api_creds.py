"""One-time setup script: derives CLOB API credentials (key/secret/passphrase)
from your wallet's private key. Polymarket doesn't hand these out via a UI -
they're created (or re-derived, if you've done this before) by signing a
request to the CLOB API with your private key.

Usage:
    uv run python scripts/generate_api_creds.py

Requires POLYMARKET_PRIVATE_KEY set in .env (and CLOB_FUNDER_ADDRESS too, if
CLOB_SIGNATURE_TYPE is 1 or 2). Prints CLOB_API_KEY / CLOB_API_SECRET /
CLOB_API_PASSPHRASE for you to paste into .env yourself - this script never
writes to .env, so it can't clobber anything already there. Safe to re-run:
the same private key + nonce always derives the same credentials.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from py_clob_client.client import ClobClient

from config import settings
from execution.client import POLYGON_CHAIN_ID


def main() -> None:
    if not settings.private_key:
        raise SystemExit("POLYMARKET_PRIVATE_KEY is not set in .env - set it first.")
    if settings.clob_signature_type in (1, 2) and not settings.clob_funder_address:
        raise SystemExit(
            f"CLOB_FUNDER_ADDRESS is required for CLOB_SIGNATURE_TYPE={settings.clob_signature_type} "
            "- set it in .env first."
        )

    client = ClobClient(
        settings.clob_api_url,
        chain_id=POLYGON_CHAIN_ID,
        key=settings.private_key,
        signature_type=settings.clob_signature_type,
        funder=settings.clob_funder_address,
    )
    creds = client.create_or_derive_api_creds()
    if creds is None:
        raise SystemExit("Failed to create or derive API credentials - check the logs above.")

    print("Derived CLOB API credentials - paste these into .env:\n")
    print(f"CLOB_API_KEY={creds.api_key}")
    print(f"CLOB_API_SECRET={creds.api_secret}")
    print(f"CLOB_API_PASSPHRASE={creds.api_passphrase}")


if __name__ == "__main__":
    main()
