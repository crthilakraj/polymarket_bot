"""Keeps tracked_markets.json pointed at currently-live sports/esports game
markets (moneylines, map/game winners, O/U) - the market category validated
in backtest to show a real, out-of-sample-replicated edge for
ComplementaryOutcomesSignal, unlike political/Fed markets which showed no
edge across extensive testing (see README).

Unlike political markets (which run for weeks/months), game markets resolve
in hours, so a static tracked list goes stale fast - this refreshes it on
an interval, dropping markets that have gone stale (little time left / no
longer active) and adding freshly-discovered live games, so a long-running
`main.py` process keeps having real, active markets to trade against
instead of idling on games that already ended.

Also (unvalidated, exploratory) tracks a second pool of niche/low-volume
markets alongside the sports pool: research on prediction-market
inefficiency reports that low-volume, less-watched markets (award shows,
niche crypto/price markets, etc.) show wider and more persistent bid/ask
gaps since fewer participants/bots are competing them away. This uses the
exact same validated ComplementaryOutcomesSignal - only the market
*selection* changes - so it's a cheap way to test whether the edge
generalizes beyond sports. Track its P&L separately (by condition_id, via
market_metadata.category or the niche pool's own tracked entries) before
trusting it the way the sports edge has been trusted.

Usage:
    uv run python scripts/refresh_live_games.py --interval-seconds 600 --target-count 10 --niche-count 5
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings  # noqa: E402

GAMMA_BASE = "https://gamma-api.polymarket.com"
MIN_HOURS_LEFT = 0.5
MAX_HOURS_LEFT = 6.0
MIN_VOLUME_24H = 50_000

# Niche/low-volume pool: markets with SOME real activity (order book enabled,
# not a dead/never-traded listing) but well below the sports pool's
# MIN_VOLUME_24H, on the theory that less-watched markets get their
# mispricings arbitraged away more slowly. Widened time window vs. the
# sports pool since these aren't tied to a single game's live clock.
NICHE_MIN_VOLUME_24H = 200
NICHE_MAX_VOLUME_24H = 20_000
NICHE_MIN_HOURS_LEFT = 2.0
NICHE_MAX_HOURS_LEFT = 30 * 24.0


def fetch_live_game_markets(client: httpx.Client, limit: int = 100) -> list[dict]:
    resp = client.get(
        f"{GAMMA_BASE}/markets",
        params={
            "active": "true",
            "closed": "false",
            "limit": limit,
            "order": "volume24hr",
            "ascending": "false",
        },
        timeout=20.0,
    )
    resp.raise_for_status()
    markets = resp.json()

    now = datetime.now(timezone.utc)
    candidates = []
    for m in markets:
        question = m.get("question", "")
        if not (" vs. " in question or " vs " in question or "Winner" in question):
            continue
        end = m.get("endDate")
        if not end:
            continue
        try:
            end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        except ValueError:
            continue
        hours_left = (end_dt - now).total_seconds() / 3600
        if not (MIN_HOURS_LEFT < hours_left < MAX_HOURS_LEFT):
            continue
        volume = m.get("volume24hr") or 0
        if volume < MIN_VOLUME_24H:
            continue
        candidates.append(
            {"condition_id": m["conditionId"], "question": question, "volume24hr": volume, "hours_left": hours_left}
        )
    candidates.sort(key=lambda c: c["volume24hr"], reverse=True)
    return candidates


def fetch_niche_markets(client: httpx.Client, max_pages: int = 10) -> list[dict]:
    """Active, order-book-enabled markets with low-but-nonzero 24h volume,
    excluding anything matching the sports/esports "vs."/"Winner" pattern so
    the two pools don't double-count the same markets. Sorted ascending by
    volume - the thinnest-but-still-live markets first, per the theory that
    less-watched markets keep persistent gaps longest.

    Gamma caps each page at 100 results regardless of the requested `limit`,
    and sorting ascending by volume24hr surfaces only dead 0-volume listings
    (nothing in NICHE_MIN_VOLUME_24H..NICHE_MAX_VOLUME_24H). So this instead
    pages descending by volume24hr via `offset` and stops once a page's
    volume drops below the floor - since results are sorted descending,
    every later page would only be lower still."""
    now = datetime.now(timezone.utc)
    candidates = []
    for page in range(max_pages):
        resp = client.get(
            f"{GAMMA_BASE}/markets",
            params={
                "active": "true",
                "closed": "false",
                "limit": 100,
                "offset": page * 100,
                "order": "volume24hr",
                "ascending": "false",
            },
            timeout=20.0,
        )
        resp.raise_for_status()
        markets = resp.json()
        if not markets:
            break

        page_min_volume = min(m.get("volume24hr") or 0 for m in markets)
        for m in markets:
            if not m.get("enableOrderBook"):
                continue
            question = m.get("question", "")
            if " vs. " in question or " vs " in question or "Winner" in question:
                continue
            end = m.get("endDate")
            if not end:
                continue
            try:
                end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
            except ValueError:
                continue
            hours_left = (end_dt - now).total_seconds() / 3600
            if not (NICHE_MIN_HOURS_LEFT < hours_left < NICHE_MAX_HOURS_LEFT):
                continue
            volume = m.get("volume24hr") or 0
            if not (NICHE_MIN_VOLUME_24H <= volume <= NICHE_MAX_VOLUME_24H):
                continue
            candidates.append(
                {
                    "condition_id": m["conditionId"],
                    "question": question,
                    "volume24hr": volume,
                    "hours_left": hours_left,
                }
            )
        if page_min_volume < NICHE_MIN_VOLUME_24H:
            break

    candidates.sort(key=lambda c: c["volume24hr"])
    return candidates


def refresh(client: httpx.Client, target_count: int, niche_count: int = 0) -> list[dict]:
    candidates = fetch_live_game_markets(client)
    picked = candidates[:target_count]
    if niche_count:
        picked = picked + fetch_niche_markets(client)[:niche_count]
    tracked = [{"condition_id": c["condition_id"], "question": c["question"]} for c in picked]
    Path(settings.tracked_markets_path).write_text(json.dumps(tracked, indent=2))
    return picked


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--interval-seconds", type=float, default=600.0)
    parser.add_argument("--target-count", type=int, default=10)
    parser.add_argument(
        "--niche-count",
        type=int,
        default=0,
        help="how many low-volume/niche markets to track alongside the sports pool (0 = disabled)",
    )
    parser.add_argument("--max-refreshes", type=int, default=0, help="0 = run forever")
    args = parser.parse_args()

    client = httpx.Client()
    count = 0
    while True:
        try:
            picked = refresh(client, args.target_count, args.niche_count)
            print(f"[{datetime.now(timezone.utc).isoformat()}] refreshed: {len(picked)} live game markets")
            for p in picked:
                print(f"  {p['hours_left']:.1f}h left | vol24h=${p['volume24hr']:,.0f} | {p['question'][:60]}")
        except httpx.HTTPError as exc:
            print(f"[{datetime.now(timezone.utc).isoformat()}] refresh failed: {exc}")
        count += 1
        if args.max_refreshes and count >= args.max_refreshes:
            break
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    main()
