"""Feasibility check using REAL executed trade prints (not live order book
snapshots, which we only have a few minutes of): pulls each tracked binary
market's full trade tape from Polymarket's public data-api
(data-api.polymarket.com/trades, paginated), merges the YES/NO tapes by
time, and measures how often and by how much sum(last_trade_price) actually
deviated from $1.00 over the real historical window.

This is a genuine feasibility check for the complementary-outcomes arb, not
a backtest: it uses last TRADE prices as a proxy for tradeable price, not
actual historical bid/ask depth (which Polymarket doesn't expose
historically), so a conservative `--slippage-bps` buffer is subtracted from
every observed deviation before it counts as "edge" - meant to cover the
real gap between a trade print and what you'd actually pay/receive crossing
the spread yourself. Report the result honestly: this either shows real,
frequent, sizeable mispricing (worth building a live execution path for) or
it shows an efficient market (most likely, since public market makers watch
exactly this signal) - either answer is useful, and this script must not be
tuned to manufacture the former.

Usage:
    uv run python scripts/analyze_trade_history.py --slippage-bps 200 --trade-size-usd 25
"""

import argparse
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.gamma_client import GammaClient  # noqa: E402
from config import settings  # noqa: E402

DATA_API_BASE = "https://data-api.polymarket.com"
PAGE_LIMIT = 500


def fetch_trades(client: httpx.Client, condition_id: str, max_pages: int = 20) -> list[dict]:
    """Paginates data-api's /trades. The API 400s once offset runs past its
    internal history depth cap rather than returning an empty page - treated
    as "no more pages", not a hard failure, so a market with a long tape
    still returns everything fetched before the cutoff instead of nothing."""
    trades: list[dict] = []
    offset = 0
    for _ in range(max_pages):
        resp = client.get(
            f"{DATA_API_BASE}/trades",
            params={"market": condition_id, "limit": PAGE_LIMIT, "offset": offset},
            timeout=20.0,
        )
        if resp.status_code == 400:
            break
        resp.raise_for_status()
        page = resp.json()
        if not page:
            break
        trades.extend(page)
        if len(page) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
    return trades


def analyze_market(
    question: str,
    trades: list[dict],
    slippage_rate: float,
    trade_size_usd: float,
    max_staleness_seconds: float,
) -> dict:
    """IMPORTANT caveat: sum(last_trade_price) drifting from $1 is NOT the
    same thing as a real, tradeable mispricing - if outcome A just traded but
    outcome B's last trade was hours ago, the "deviation" is often just B's
    stale print, not B's actual current book (illiquid outcomes' books get
    repriced by market makers without needing a trade to print). Requiring
    both legs' last trade to be within max_staleness_seconds of the
    triggering trade is a blunt but honest filter for this: it can't prove
    the deviation was tradeable (we don't have historical order books), but
    it rules out the most obvious way this analysis overstates edge.
    """
    trades = sorted(trades, key=lambda t: t["timestamp"])
    if not trades:
        return {"question": question, "num_trades": 0, "events": [], "span_days": 0.0}

    outcomes = sorted({t["asset"] for t in trades})
    if len(outcomes) != 2:
        return {"question": question, "num_trades": len(trades), "events": [], "span_days": 0.0, "skipped": "not exactly 2 outcomes"}

    last_price: dict[str, float] = {}
    last_ts: dict[str, float] = {}
    events = []
    for t in trades:
        last_price[t["asset"]] = t["price"]
        last_ts[t["asset"]] = t["timestamp"]
        if len(last_price) < 2:
            continue
        staleness = max(t["timestamp"] - ts for ts in last_ts.values())
        if staleness > max_staleness_seconds:
            continue  # one leg's price is too old to trust as "current"
        total = sum(last_price.values())
        deviation = 1.0 - total  # >0: complete set underpriced (buy); <0: overpriced (sell)
        edge = abs(deviation) - slippage_rate
        if edge > 0:
            profit = edge * trade_size_usd
            events.append(
                {
                    "timestamp": t["timestamp"],
                    "sum_price": total,
                    "raw_deviation": deviation,
                    "edge_after_slippage": edge,
                    "profit_usd": profit,
                }
            )

    span_days = (trades[-1]["timestamp"] - trades[0]["timestamp"]) / 86400 if len(trades) > 1 else 0.0
    return {
        "question": question,
        "num_trades": len(trades),
        "span_days": span_days,
        "events": events,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--slippage-bps", type=float, default=200.0, help="Conservative round-trip slippage buffer subtracted from every observed deviation")
    parser.add_argument("--trade-size-usd", type=float, default=25.0, help="Hypothetical notional per leg, for profit estimation")
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument(
        "--max-staleness-seconds",
        type=float,
        default=300.0,
        help="Both legs' last trade must be within this many seconds of each other to count as a real deviation, not a stale print",
    )
    args = parser.parse_args()

    import json
    tracked = json.loads(Path(settings.tracked_markets_path).read_text())
    condition_ids = [m["condition_id"] for m in tracked]
    questions = {m["condition_id"]: m["question"] for m in tracked}

    slippage_rate = args.slippage_bps / 10_000

    http_client = httpx.Client()
    all_events = []
    total_span_days = 0.0
    print(f"Analyzing {len(condition_ids)} tracked markets' real trade history (slippage buffer={args.slippage_bps}bps)...\n")
    for cid in condition_ids:
        try:
            trades = fetch_trades(http_client, cid, max_pages=args.max_pages)
        except httpx.HTTPError as exc:
            print(f"  {questions[cid][:60]:<60} fetch failed: {exc}")
            continue
        result = analyze_market(
            questions[cid], trades, slippage_rate, args.trade_size_usd, args.max_staleness_seconds
        )
        total_span_days = max(total_span_days, result["span_days"])
        n_events = len(result["events"])
        flag = result.get("skipped", "")
        print(
            f"  {result['question'][:60]:<60} trades={result['num_trades']:>5} "
            f"span={result['span_days']:>6.1f}d real_arb_events={n_events:>4} {flag}"
        )
        for e in result["events"]:
            e["question"] = result["question"]
            all_events.append(e)
        time.sleep(0.2)  # be polite to the public API

    print(f"\nTotal real arb events across all tracked markets: {len(all_events)}")
    if all_events:
        total_profit = sum(e["profit_usd"] for e in all_events)
        print(f"Total hypothetical profit (${args.trade_size_usd}/leg, net of {args.slippage_bps}bps slippage buffer): ${total_profit:.2f}")
        by_day = defaultdict(float)
        for e in all_events:
            day = datetime.fromtimestamp(e["timestamp"], tz=timezone.utc).date()
            by_day[day] += e["profit_usd"]
        print(f"Days with at least one real event: {len(by_day)} out of ~{total_span_days:.0f} days observed")
        for day in sorted(by_day):
            print(f"  {day}: ${by_day[day]:.2f}")
        if total_span_days > 0:
            avg_daily = total_profit / total_span_days
            print(f"\nAverage $/day across the full observed window: ${avg_daily:.2f}")
    else:
        print(
            "No real historical deviation exceeded the slippage buffer on any tracked market. "
            "This is evidence the complementary-outcomes arb is not present at a tradeable size "
            "on these markets over this window - not a bug."
        )


if __name__ == "__main__":
    main()
