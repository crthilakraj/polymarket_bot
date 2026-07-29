"""Periodically re-fetches Gamma metadata for EVERY condition_id ever
collected into the DB - not just whatever's currently in
tracked_markets.json - so that a market's eventual `closed=true` /
outcome_prices update gets picked up even after it rotates out of the live
game rotation (scripts/refresh_live_games.py + run_live_games_loop.sh only
refresh metadata for markets they're *currently* tracking; once a game ends
and gets replaced by a fresher one, its metadata was otherwise frozen at
whatever it looked like the last time it was tracked - so a real resolution
happening after that point was invisible to backtest/report_cumulative_arb_pnl.py's
settlement logic, which only settles positions once `closed=true`).

This runs independently of the main.py rotation loop and only touches
market_metadata (never places orders, never touches order_book_snapshots),
so it's safe to run alongside scripts/run_live_games_loop.sh.

Usage:
    uv run python scripts/refresh_all_metadata.py --interval-seconds 300
"""

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.gamma_client import GammaClient  # noqa: E402
from data.store import DataStore  # noqa: E402
from config import settings  # noqa: E402

BATCH_SIZE = 20  # Gamma's condition_ids query param gets unwieldy past this


def refresh_once(store: DataStore, gamma: GammaClient) -> tuple[int, int]:
    rows = store._conn.execute("SELECT condition_id FROM market_metadata").fetchall()
    condition_ids = [r["condition_id"] for r in rows]

    newly_closed = 0
    for i in range(0, len(condition_ids), BATCH_SIZE):
        batch = condition_ids[i : i + BATCH_SIZE]
        before_closed = {
            cid: store._conn.execute(
                "SELECT closed FROM market_metadata WHERE condition_id=?", (cid,)
            ).fetchone()["closed"]
            for cid in batch
        }
        try:
            markets = gamma.get_markets_by_condition_ids(batch)
        except Exception as exc:  # noqa: BLE001 - keep the refresh loop alive on any transient API failure
            print(f"[{datetime.now(timezone.utc).isoformat()}] batch fetch failed: {exc}")
            continue
        for market in markets:
            store.save_market_metadata(market)
            if market.closed and not before_closed.get(market.condition_id):
                newly_closed += 1
        time.sleep(0.2)

    return len(condition_ids), newly_closed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--interval-seconds", type=float, default=300.0)
    parser.add_argument("--max-refreshes", type=int, default=0, help="0 = run forever")
    args = parser.parse_args()

    store = DataStore(settings.db_path)
    gamma = GammaClient()
    count = 0
    try:
        while True:
            try:
                total, newly_closed = refresh_once(store, gamma)
                print(
                    f"[{datetime.now(timezone.utc).isoformat()}] refreshed {total} markets, "
                    f"{newly_closed} newly closed this pass"
                )
            except Exception as exc:  # noqa: BLE001 - a transient failure (e.g. a locked DB
                # while checkpoint_and_prune.py holds a long write transaction) shouldn't kill
                # this long-running loop; log and retry next cycle instead.
                print(f"[{datetime.now(timezone.utc).isoformat()}] refresh cycle failed: {exc}")
            count += 1
            if args.max_refreshes and count >= args.max_refreshes:
                break
            time.sleep(args.interval_seconds)
    finally:
        store.close()
        gamma.close()


if __name__ == "__main__":
    main()
