"""Central config: API keys and risk limits, loaded from environment / .env.

Import `settings` from this module rather than reading os.environ directly
elsewhere, so all configuration stays in one place.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _parse_id_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _load_tracked_market_ids(path: str, fallback: list[str]) -> list[str]:
    """condition_ids from the tracked-markets file (written by
    data.market_screener.write_tracked_markets - see its docstring for the
    format) if it exists and parses, else `fallback` (MARKET_CONDITION_IDS).
    Malformed/unreadable files fall back too rather than crashing config
    import - this file is meant to be hand-edited."""
    file_path = Path(path)
    if not file_path.exists():
        return fallback
    try:
        entries = json.loads(file_path.read_text())
    except (json.JSONDecodeError, OSError):
        return fallback
    ids = [
        entry["condition_id"]
        for entry in entries
        if isinstance(entry, dict) and entry.get("condition_id")
    ]
    return ids or fallback


@dataclass(frozen=True)
class Settings:
    # Polymarket CLOB API credentials
    clob_api_url: str
    gamma_api_url: str
    clob_ws_market_url: str
    private_key: str | None
    clob_api_key: str | None
    clob_api_secret: str | None
    clob_api_passphrase: str | None
    clob_signature_type: int
    clob_funder_address: str | None

    # Risk limits
    max_position_usd: float
    max_order_usd: float
    max_daily_loss_usd: float
    # Bankroll cap used for sizing/exposure when DRY_RUN=true (paper trading -
    # no real funds involved, purely a simulated ceiling).
    max_portfolio_exposure_usd: float
    # Bankroll cap used for sizing/exposure when DRY_RUN=false (live trading
    # with real funds). Kept separate from max_portfolio_exposure_usd so
    # paper-trading experiments never accidentally influence the real-money
    # cap, and vice versa. OrderManager also clamps this further against the
    # actual live USDC balance queried from Polymarket - see
    # execution.client.get_collateral_balance_usd - so this is a ceiling,
    # not a guarantee that much is actually available.
    live_max_fund_usd: float
    kelly_fraction: float

    # Data layer
    market_condition_ids: list[str]
    tracked_markets_path: str
    db_path: str
    gamma_refresh_interval_seconds: float

    # News edge signal
    claude_model: str
    news_embedding_model: str
    news_similarity_threshold: float
    news_min_probability_shift: float

    # Misc
    log_level: str
    dry_run: bool
    live_trading_confirmed: bool
    enable_market_making: bool

    @classmethod
    def from_env(cls) -> "Settings":
        """Build Settings by reading the current environment (call after load_dotenv())."""
        return cls(
            clob_api_url=os.getenv("CLOB_API_URL", "https://clob.polymarket.com"),
            gamma_api_url=os.getenv("GAMMA_API_URL", "https://gamma-api.polymarket.com"),
            clob_ws_market_url=os.getenv(
                "CLOB_WS_MARKET_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/market"
            ),
            private_key=os.getenv("POLYMARKET_PRIVATE_KEY"),
            clob_api_key=os.getenv("CLOB_API_KEY"),
            clob_api_secret=os.getenv("CLOB_API_SECRET"),
            clob_api_passphrase=os.getenv("CLOB_API_PASSPHRASE"),
            clob_signature_type=int(os.getenv("CLOB_SIGNATURE_TYPE", "0")),
            clob_funder_address=os.getenv("CLOB_FUNDER_ADDRESS"),
            max_position_usd=float(os.getenv("MAX_POSITION_USD", "100")),
            max_order_usd=float(os.getenv("MAX_ORDER_USD", "25")),
            max_daily_loss_usd=float(os.getenv("MAX_DAILY_LOSS_USD", "50")),
            max_portfolio_exposure_usd=float(os.getenv("MAX_PORTFOLIO_EXPOSURE_USD", "300")),
            live_max_fund_usd=float(os.getenv("LIVE_MAX_FUND_USD", "300")),
            kelly_fraction=float(os.getenv("KELLY_FRACTION", "0.25")),
            market_condition_ids=_load_tracked_market_ids(
                os.getenv("TRACKED_MARKETS_PATH", "tracked_markets.json"),
                _parse_id_list(os.getenv("MARKET_CONDITION_IDS")),
            ),
            tracked_markets_path=os.getenv("TRACKED_MARKETS_PATH", "tracked_markets.json"),
            db_path=os.getenv("DB_PATH", "polymarket_data.db"),
            gamma_refresh_interval_seconds=float(
                os.getenv("GAMMA_REFRESH_INTERVAL_SECONDS", "300")
            ),
            claude_model=os.getenv("CLAUDE_MODEL", "claude-opus-4-8"),
            news_embedding_model=os.getenv("NEWS_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5"),
            news_similarity_threshold=float(os.getenv("NEWS_SIMILARITY_THRESHOLD", "0.5")),
            news_min_probability_shift=float(os.getenv("NEWS_MIN_PROBABILITY_SHIFT", "0.03")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            dry_run=os.getenv("DRY_RUN", "true").lower() in ("1", "true", "yes"),
            live_trading_confirmed=os.getenv("LIVE_TRADING_CONFIRMED", "false").lower()
            in ("1", "true", "yes"),
            enable_market_making=os.getenv("ENABLE_MARKET_MAKING", "false").lower()
            in ("1", "true", "yes"),
        )

    def require_trading_credentials(self) -> None:
        """Raise if credentials needed for live (non-dry-run) trading are missing."""
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

    def require_live_trading_confirmation(self) -> None:
        """Raise unless the user has explicitly opted into live trading.

        DRY_RUN=false alone isn't enough - LIVE_TRADING_CONFIRMED must also be
        set, so a single forgotten/misconfigured flag can't silently turn on
        real order placement. A no-op when dry_run is True."""
        if self.dry_run:
            return
        if not self.live_trading_confirmed:
            raise RuntimeError(
                "DRY_RUN=false but LIVE_TRADING_CONFIRMED is not set. Live trading requires "
                "both to be explicitly set - add LIVE_TRADING_CONFIRMED=true to .env only "
                "once you intend to place real orders."
            )
        self.require_trading_credentials()


settings = Settings.from_env()
