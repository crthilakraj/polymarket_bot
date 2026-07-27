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

Usage:
    uv run python scripts/refresh_live_games.py --interval-seconds 600 --target-count 10
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


def refresh(client: httpx.Client, target_count: int) -> list[dict]:
    candidates = fetch_live_game_markets(client)
    picked = candidates[:target_count]
    tracked = [{"condition_id": c["condition_id"], "question": c["question"]} for c in picked]
    Path(settings.tracked_markets_path).write_text(json.dumps(tracked, indent=2))
    return picked


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--interval-seconds", type=float, default=600.0)
    parser.add_argument("--target-count", type=int, default=10)
    parser.add_argument("--max-refreshes", type=int, default=0, help="0 = run forever")
    args = parser.parse_args()

    client = httpx.Client()
    count = 0
    while True:
        try:
            picked = refresh(client, args.target_count)
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
