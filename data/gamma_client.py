"""Gamma API client: market metadata (question, end date, resolution criteria,
category, outcomes, and the CLOB token ids needed to subscribe on the WS feed).

Docs: https://docs.polymarket.com (Gamma API), base URL https://gamma-api.polymarket.com,
no auth required for reads. No published rate limit, but aggressive polling can be
throttled (429), so requests here retry with exponential backoff.
"""

import json
import logging
import time
from datetime import datetime

import httpx

from config import settings
from data.models import MarketMetadata

logger = logging.getLogger(__name__)

MAX_RETRIES = 5
INITIAL_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 30.0

DEFAULT_EVENTS_PAGE_LIMIT = 100
DEFAULT_MAX_MARKETS = 2000


def _parse_json_list(value) -> list:
    """Gamma returns outcomes/outcomePrices/clobTokenIds as JSON-encoded strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return json.loads(value)
    return list(value)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _to_market_metadata(raw: dict) -> MarketMetadata:
    return MarketMetadata(
        condition_id=raw["conditionId"],
        question_id=raw.get("questionID"),
        question=raw.get("question"),
        description=raw.get("description"),
        resolution_source=raw.get("resolutionSource"),
        category=raw.get("category"),
        end_date=_parse_datetime(raw.get("endDate")),
        active=raw.get("active"),
        closed=raw.get("closed"),
        outcomes=_parse_json_list(raw.get("outcomes")),
        outcome_prices=[float(p) for p in _parse_json_list(raw.get("outcomePrices"))],
        token_ids=_parse_json_list(raw.get("clobTokenIds")),
    )


class GammaClient:
    """Thin, retrying HTTP client for the Gamma API's public read endpoints."""

    def __init__(self, base_url: str | None = None, client: httpx.Client | None = None):
        self._base_url = base_url or settings.gamma_api_url
        self._client = client or httpx.Client(timeout=15.0)

    def _get_with_retry(self, path: str, params: dict) -> httpx.Response:
        backoff = INITIAL_BACKOFF_SECONDS
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = self._client.get(f"{self._base_url}{path}", params=params)
            except httpx.TransportError as exc:
                last_exc = exc
                logger.warning(
                    "gamma request failed (attempt %d/%d): %s", attempt, MAX_RETRIES, exc
                )
            else:
                if response.status_code == 429 or response.status_code >= 500:
                    retry_after = response.headers.get("Retry-After")
                    wait = float(retry_after) if retry_after else backoff
                    logger.warning(
                        "gamma returned HTTP %d (attempt %d/%d), backing off %.1fs",
                        response.status_code,
                        attempt,
                        MAX_RETRIES,
                        wait,
                    )
                    time.sleep(wait)
                    backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                    continue
                response.raise_for_status()
                return response
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
        raise RuntimeError(f"Gamma request to {path} failed after {MAX_RETRIES} attempts") from last_exc

    def get_markets_by_condition_ids(self, condition_ids: list[str]) -> list[MarketMetadata]:
        """Fetch current metadata for the given condition_ids (order not guaranteed).

        Gamma's /markets endpoint defaults to closed=false when the `closed`
        param is omitted - a condition_ids-only query silently excludes any
        market that has since resolved, so a caller polling for resolutions
        (e.g. scripts/refresh_all_metadata.py) would never see closed=true
        even after checking forever. `closed` also only accepts a single
        value, not a list, so both states have to be fetched as separate
        requests and merged rather than in one call.
        """
        if not condition_ids:
            return []
        markets: list[MarketMetadata] = []
        for closed in ("false", "true"):
            response = self._get_with_retry(
                "/markets", params={"condition_ids": condition_ids, "closed": closed}
            )
            markets.extend(_to_market_metadata(raw) for raw in response.json())
        return markets

    def get_active_events(
        self,
        page_limit: int = DEFAULT_EVENTS_PAGE_LIMIT,
        max_markets: int = DEFAULT_MAX_MARKETS,
    ) -> list[dict]:
        """Raw event JSON for every active, non-closed event, paginated via
        offset until a short page comes back (or max_markets is hit). Each
        event embeds its own markets[] and tags[] - used by
        data.market_screener to build a filterable/rankable shortlist
        without one API call per market. Returns raw dicts (not
        MarketMetadata) since screening needs fields (volume, liquidity,
        tags) that MarketMetadata doesn't carry."""
        events: list[dict] = []
        total_markets = 0
        offset = 0
        while total_markets < max_markets:
            response = self._get_with_retry(
                "/events",
                params={"active": True, "closed": False, "limit": page_limit, "offset": offset},
            )
            page = response.json()
            if not page:
                break
            events.extend(page)
            total_markets += sum(len(event.get("markets", [])) for event in page)
            if len(page) < page_limit:
                break
            offset += page_limit
        return events

    def close(self) -> None:
        self._client.close()
