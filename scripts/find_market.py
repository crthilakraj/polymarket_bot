"""Look up a market's condition_id from its Polymarket URL/slug or a search
term, so you can populate MARKET_CONDITION_IDS in .env. Read-only, no auth
required.

Usage:
    uv run python scripts/find_market.py "fed interest rate"
    uv run python scripts/find_market.py https://polymarket.com/event/fed-decision-in-october
    uv run python scripts/find_market.py fed-decision-in-october
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from config import settings


def _extract_slug(text: str) -> str | None:
    match = re.search(r"polymarket\.com/event/([^/?#]+)", text)
    if match:
        return match.group(1)
    if re.fullmatch(r"[a-z0-9-]+", text):
        return text
    return None


def _print_market(market: dict) -> None:
    print(f"  {market.get('question')}")
    print(f"    condition_id: {market.get('conditionId')}")
    print(f"    slug:         {market.get('slug')}")
    print(f"    active={market.get('active')} closed={market.get('closed')}")
    print()


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(
            "Usage: uv run python scripts/find_market.py <search term | slug | polymarket.com URL>"
        )
    query = sys.argv[1]

    with httpx.Client(timeout=15.0) as client:
        slug = _extract_slug(query)
        if slug:
            resp = client.get(f"{settings.gamma_api_url}/markets", params={"slug": slug})
            resp.raise_for_status()
            markets = resp.json()
            if markets:
                print(f"Found {len(markets)} market(s) for slug={slug!r}:\n")
                for market in markets:
                    _print_market(market)
                return
            print(f"No market found for slug={slug!r} - falling back to search.\n")

        resp = client.get(f"{settings.gamma_api_url}/public-search", params={"q": query, "limit_per_type": 5})
        resp.raise_for_status()
        events = resp.json().get("events", [])
        if not events:
            print("No results.")
            return
        for event in events:
            print(f"{event.get('title')}  (event slug: {event.get('slug')})")
            for market in event.get("markets", []):
                _print_market(market)


if __name__ == "__main__":
    main()
