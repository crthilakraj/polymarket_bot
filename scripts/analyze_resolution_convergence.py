"""Tests a specific, well-documented real anomaly in prediction markets (the
"favorite-longshot bias"/late-resolution convergence gap): near-certain
outcomes often don't fully converge to $1 (or $0) until the very last
moment before resolution, leaving a small, low-risk gap that can be bought
cheaply and held to settlement.

Uses real recently-CLOSED markets from Gamma (so the true outcome is known)
and their real price history (CLOB /prices-history) in the final hours
before close. For the outcome that actually won, this measures the largest
gap between its price and $1.00 within a short pre-resolution window - if
that gap, net of a conservative round-trip cost, is consistently positive
and large enough across many independent resolved markets, that's a real,
repeatable edge distinct from arb/MM/momentum (already tested and found
unprofitable).

Important honesty caveats this script does NOT hide:
  - This has hindsight: it only looks at markets we already know resolved,
    so by construction, "buy the eventual winner cheap" always looks great
    in retrospect. The real question is whether, in real time, you could
    identify which near-certain-but-not-100% market was actually going to
    win vs. one where the gap was gap because of genuine remaining
    uncertainty (a coup could still fail, a vote could still flip). A
    market open near 95% is not necessarily "97% chance, 3% mispriced" -
    it may genuinely be 95% likely, in which case buying at 95c has
    ZERO edge on average even though every individual winner you look back
    at appears to have been "underpriced."
  - This script reports what full hindsight suggests was left on the
    table - a ceiling on the OPPORTUNITY, not a proof that it was
    exploitable by anyone without hindsight.

Usage:
    uv run python scripts/analyze_resolution_convergence.py --num-markets 100 --window-hours 6
"""

import argparse
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"


def fetch_recently_closed_binary_markets(client: httpx.Client, limit: int) -> list[dict]:
    resp = client.get(
        f"{GAMMA_BASE}/markets",
        params={"closed": "true", "limit": limit, "order": "endDate", "ascending": "false"},
        timeout=20.0,
    )
    resp.raise_for_status()
    markets = resp.json()
    out = []
    for m in markets:
        try:
            outcomes = m.get("outcomes")
            prices = m.get("outcomePrices")
            tokens = m.get("clobTokenIds")
            if isinstance(outcomes, str):
                import json as _json

                outcomes = _json.loads(outcomes)
                prices = _json.loads(prices)
                tokens = _json.loads(tokens)
            if len(outcomes) != 2 or len(tokens) != 2:
                continue
            prices = [float(p) for p in prices]
            winner_idx = 0 if prices[0] > prices[1] else 1
            out.append({"question": m["question"], "winner_token": tokens[winner_idx], "winner_price": prices[winner_idx]})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--num-markets", type=int, default=100)
    parser.add_argument("--window-hours", type=float, default=6.0, help="Look at the final N hours of price history before market close")
    parser.add_argument("--round-trip-cost-bps", type=float, default=676.0)
    parser.add_argument("--trade-size-usd", type=float, default=25.0)
    args = parser.parse_args()

    client = httpx.Client()
    markets = fetch_recently_closed_binary_markets(client, args.num_markets)
    print(f"Found {len(markets)} recently-closed binary markets. Checking final {args.window_hours}h price history for each...\n")

    cost_rate = args.round_trip_cost_bps / 10_000
    results = []
    raw_gaps = []
    for m in markets:
        try:
            resp = client.get(
                f"{CLOB_BASE}/prices-history",
                params={"market": m["winner_token"], "interval": "max", "fidelity": 5},
                timeout=15.0,
            )
            if resp.status_code != 200:
                continue
            history = resp.json().get("history", [])
        except httpx.HTTPError:
            continue
        if len(history) < 2:
            continue
        window_start_ts = history[-1]["t"] - int(args.window_hours * 3600)
        window_prices = [h["p"] for h in history if h["t"] >= window_start_ts]
        if not window_prices:
            continue
        min_price = min(window_prices)
        gap = 1.0 - min_price
        edge_after_cost = gap - cost_rate
        raw_gaps.append(gap)
        if edge_after_cost > 0:
            profit = edge_after_cost * args.trade_size_usd
            results.append({"question": m["question"], "min_price": min_price, "gap": gap, "profit_usd": profit})
        time.sleep(0.1)

    print(f"Markets with a real (hindsight) profitable gap after {args.round_trip_cost_bps}bps cost: {len(results)} / {len(markets)}\n")
    results.sort(key=lambda r: r["profit_usd"], reverse=True)
    total = 0.0
    for r in results:
        print(f"  {r['question'][:60]:<60} min_price={r['min_price']:.3f} gap={r['gap']:.1%} profit=${r['profit_usd']:.2f}")
        total += r["profit_usd"]
    print(f"\nTotal hindsight profit across {len(results)} resolved markets: ${total:.2f} (${args.trade_size_usd}/trade)")
    if raw_gaps:
        raw_gaps.sort()
        n = len(raw_gaps)
        print(
            f"\nRaw gap distribution (before cost, n={n}): "
            f"median={raw_gaps[n//2]:.2%} p75={raw_gaps[3*n//4]:.2%} p90={raw_gaps[int(n*0.9)]:.2%} max={raw_gaps[-1]:.2%}"
        )
    print(
        "\nReminder: this is a hindsight ceiling, not a real-time-executable edge - see the docstring. "
        "A real strategy would need a way to distinguish 'genuinely still uncertain at 95c' from "
        "'mispriced at 95c' BEFORE the outcome is known, which this script cannot test."
    )


if __name__ == "__main__":
    main()
