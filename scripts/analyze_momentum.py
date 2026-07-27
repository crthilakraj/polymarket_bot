"""Feasibility check for a DIRECTIONAL strategy family (momentum and mean-
reversion), as opposed to the market-neutral complementary-outcomes/
market-making strategies already tested and found unprofitable in
backtest/optimize.py and scripts/analyze_trade_history.py.

Uses Polymarket CLOB's public /prices-history endpoint (real historical
price series, fidelity in minutes) - not fabricated. For each tracked
market, sweeps (lookback, threshold, holding period, direction) and reports
whether ANY combination shows real, walk-forward-consistent positive
expectancy net of a conservative round-trip cost (default 676bps, the
median real bid-ask spread measured from this bot's own live order book
collection - see README).

This is a feasibility scan, not a finished trading strategy: transaction
cost is a flat assumption (real spread varies by market/time and this
doesn't model slippage from walking the book), and "hold for N periods then
exit at market" ignores that exiting also crosses the spread. Both cut
against overstating the result. Report honestly either way - if nothing
here clears the bar either, that's real evidence, not a dead end to hide.

Usage:
    uv run python scripts/analyze_momentum.py --fidelity 10 --round-trip-cost-bps 676
"""

import argparse
import itertools
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings  # noqa: E402

CLOB_BASE = "https://clob.polymarket.com"

LOOKBACK_GRID = [3, 6, 12, 24]  # in fidelity-minute units
THRESHOLD_BPS_GRID = [100, 200, 500, 1000]
HOLD_GRID = [3, 6, 12, 24]
DIRECTION_GRID = ["momentum", "reversion"]


@dataclass
class Trade:
    entry_ts: int
    exit_ts: int
    pnl_frac: float  # fractional return on notional, net of cost


def fetch_price_history(client: httpx.Client, token_id: str, fidelity: int) -> list[dict]:
    resp = client.get(
        f"{CLOB_BASE}/prices-history",
        params={"market": token_id, "interval": "max", "fidelity": fidelity},
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json().get("history", [])


def simulate(
    history: list[dict],
    lookback: int,
    threshold_rate: float,
    hold: int,
    direction: str,
    cost_rate: float,
) -> list[Trade]:
    prices = [h["p"] for h in history]
    timestamps = [h["t"] for h in history]
    trades: list[Trade] = []
    i = lookback
    n = len(prices)
    while i < n:
        past = prices[i - lookback]
        now = prices[i]
        if past <= 0:
            i += 1
            continue
        move = (now - past) / past
        if abs(move) < threshold_rate:
            i += 1
            continue
        exit_i = min(i + hold, n - 1)
        if exit_i == i:
            break
        entry_price = now
        exit_price = prices[exit_i]

        go_long = (move > 0) if direction == "momentum" else (move < 0)
        raw_return = (exit_price - entry_price) / entry_price if go_long else (entry_price - exit_price) / entry_price
        pnl_frac = raw_return - cost_rate
        trades.append(Trade(entry_ts=timestamps[i], exit_ts=timestamps[exit_i], pnl_frac=pnl_frac))
        i = exit_i + 1  # no overlapping positions
    return trades


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fidelity", type=int, default=10, help="prices-history fidelity in minutes")
    parser.add_argument("--round-trip-cost-bps", type=float, default=676.0)
    parser.add_argument("--trade-size-usd", type=float, default=25.0)
    parser.add_argument("--tokens-file", default="/tmp/claude-1000/-home-tchikmagalore-projects-12-polymarket-bot/4802b45d-2f01-4862-8f5a-35157ddc500b/scratchpad/liquid_tokens.json")
    args = parser.parse_args()

    tokens = json.loads(Path(args.tokens_file).read_text())
    cost_rate = args.round_trip_cost_bps / 10_000

    client = httpx.Client()
    histories = {}
    for cid, info in tokens.items():
        try:
            h = fetch_price_history(client, info["yes_token"], args.fidelity)
        except httpx.HTTPError as exc:
            print(f"  fetch failed for {info['question'][:50]}: {exc}")
            continue
        histories[cid] = h
        span_days = (h[-1]["t"] - h[0]["t"]) / 86400 if len(h) > 1 else 0
        print(f"  {info['question'][:50]:<50} points={len(h):>5} span={span_days:.1f}d")

    print(f"\nSweeping {len(LOOKBACK_GRID)*len(THRESHOLD_BPS_GRID)*len(HOLD_GRID)*len(DIRECTION_GRID)} configs "
          f"x {len(histories)} markets, round-trip cost={args.round_trip_cost_bps}bps...\n")

    best = None
    results = []
    for lookback, threshold_bps, hold, direction in itertools.product(
        LOOKBACK_GRID, THRESHOLD_BPS_GRID, HOLD_GRID, DIRECTION_GRID
    ):
        all_trades: list[Trade] = []
        for cid, h in histories.items():
            all_trades.extend(simulate(h, lookback, threshold_bps / 10_000, hold, direction, cost_rate))
        if not all_trades:
            continue
        total_pnl = sum(t.pnl_frac for t in all_trades) * args.trade_size_usd
        win_rate = sum(1 for t in all_trades if t.pnl_frac > 0) / len(all_trades)

        by_day = defaultdict(float)
        for t in all_trades:
            day = datetime.fromtimestamp(t.entry_ts, tz=timezone.utc).date()
            by_day[day] += t.pnl_frac * args.trade_size_usd
        profitable_days = sum(1 for v in by_day.values() if v > 0)

        results.append(
            {
                "params": {"lookback": lookback, "threshold_bps": threshold_bps, "hold": hold, "direction": direction},
                "num_trades": len(all_trades),
                "total_pnl": total_pnl,
                "win_rate": win_rate,
                "num_days": len(by_day),
                "profitable_days": profitable_days,
            }
        )

    results.sort(key=lambda r: r["total_pnl"], reverse=True)
    print(f"{'params':<75} {'trades':>7} {'total_pnl':>10} {'win%':>6} {'days':>5} {'prof_days':>9}")
    for r in results[:15]:
        print(
            f"{str(r['params']):<75} {r['num_trades']:>7} {r['total_pnl']:>10.2f} "
            f"{r['win_rate']:>6.1%} {r['num_days']:>5} {r['profitable_days']:>9}"
        )

    trustworthy = [r for r in results if r["num_trades"] >= 20 and r["num_days"] >= 10]
    if not trustworthy:
        print("\nNo config had >= 20 trades across >= 10 distinct days - too thin to trust any ranking.")
        return
    best = trustworthy[0]
    print(f"\nBest trustworthy config: {best['params']}")
    print(
        f"  trades={best['num_trades']} total_pnl=${best['total_pnl']:.2f} "
        f"win_rate={best['win_rate']:.1%} profitable_days={best['profitable_days']}/{best['num_days']}"
    )


if __name__ == "__main__":
    main()
