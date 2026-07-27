"""Standalone runner for the data layer: streams live order books for the
markets configured via MARKET_CONDITION_IDS and logs every update, so you can
verify the WebSocket + Gamma clients work against real Polymarket data.

Usage:
    uv run python scripts/run_data_layer.py

Requires MARKET_CONDITION_IDS to be set in .env (comma-separated condition_ids,
e.g. from a market's page on polymarket.com). Snapshots are persisted to the
SQLite file at DB_PATH (default polymarket_data.db) as they arrive. Stop with
Ctrl+C.
"""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings
from data.ingest import run
from logging_config import configure_logging

logger = logging.getLogger(__name__)


async def main() -> None:
    configure_logging()

    if not settings.market_condition_ids:
        raise SystemExit(
            "MARKET_CONDITION_IDS is not set. Add a comma-separated list of condition_ids "
            "to .env, e.g. MARKET_CONDITION_IDS=0xabc...,0xdef..."
        )

    logger.info(
        "starting data layer: %d market(s), db=%s, gamma_refresh=%ss",
        len(settings.market_condition_ids),
        settings.db_path,
        settings.gamma_refresh_interval_seconds,
    )
    await run(
        condition_ids=settings.market_condition_ids,
        db_path=settings.db_path,
        gamma_refresh_interval=settings.gamma_refresh_interval_seconds,
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("stopped by user")
