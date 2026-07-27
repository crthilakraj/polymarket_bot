"""Screens Gamma API markets down to a ranked shortlist for picking an
initial test set by hand. Queries every active, non-closed event
(GammaClient.get_active_events - paginated, retrying), filters each of its
markets against configurable thresholds, and prints the survivors as JSON,
ranked by 24h volume.

Usage:
    uv run python data/market_screener.py
    uv run python data/market_screener.py --min-volume-24h 5000 --min-liquidity 1000 \\
        --min-days 1 --max-days 30 --max-outcomes 2 --category Politics --limit 20

Note on "category": Gamma's per-market `category` field is no longer
populated (confirmed empty on live data) - the closest real equivalent is
each market's event's free-form tags (e.g. "Politics", "Sports", "Crypto").
--category matches case-insensitively against that tag list; the full tag
list is included in the output as `category` so you can see what's actually
there and adjust.

Pass --write-tracked-markets to also merge the shortlist into
config.settings.tracked_markets_path (tracked_markets.json by default) -
the file config.py reads MARKET_CONDITION_IDS from when it exists, so
main.py/scripts/run_data_layer.py pick up your picks without hand-copying
condition_ids into .env. It's a small, plain JSON file - open it afterwards
and delete any entries you don't actually want before running the bot; this
script only ever adds to it, never removes.
"""

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings  # noqa: E402
from data.gamma_client import GammaClient  # noqa: E402

DEFAULT_LIMIT = 25


@dataclass(frozen=True)
class ScreenerCriteria:
    min_volume_24h: float = 0.0
    min_liquidity: float = 0.0
    min_days_to_resolution: float | None = None
    max_days_to_resolution: float | None = None
    min_outcome_count: int | None = None
    max_outcome_count: int | None = None
    category: str | None = None  # case-insensitive match against the market's tags


@dataclass(frozen=True)
class MarketCandidate:
    question: str
    condition_id: str
    category: list[str]
    volume_24h: float
    liquidity: float
    days_to_resolution: float | None
    outcome_count: int

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "condition_id": self.condition_id,
            "category": self.category,
            "volume_24h": round(self.volume_24h, 2),
            "liquidity": round(self.liquidity, 2),
            "days_to_resolution": (
                round(self.days_to_resolution, 1) if self.days_to_resolution is not None else None
            ),
            "outcome_count": self.outcome_count,
        }


def passes_criteria(candidate: MarketCandidate, criteria: ScreenerCriteria) -> bool:
    if candidate.volume_24h < criteria.min_volume_24h:
        return False
    if candidate.liquidity < criteria.min_liquidity:
        return False

    if criteria.min_days_to_resolution is not None or criteria.max_days_to_resolution is not None:
        if candidate.days_to_resolution is None:
            return False  # can't evaluate a days-to-resolution bound without a known end date
        if (
            criteria.min_days_to_resolution is not None
            and candidate.days_to_resolution < criteria.min_days_to_resolution
        ):
            return False
        if (
            criteria.max_days_to_resolution is not None
            and candidate.days_to_resolution > criteria.max_days_to_resolution
        ):
            return False

    if criteria.min_outcome_count is not None and candidate.outcome_count < criteria.min_outcome_count:
        return False
    if criteria.max_outcome_count is not None and candidate.outcome_count > criteria.max_outcome_count:
        return False

    if criteria.category is not None:
        if not any(tag.lower() == criteria.category.lower() for tag in candidate.category):
            return False

    return True


def rank_markets(
    candidates: list[MarketCandidate], criteria: ScreenerCriteria, limit: int | None = None
) -> list[MarketCandidate]:
    """Filter to candidates passing `criteria`, ranked by 24h volume descending."""
    filtered = [c for c in candidates if passes_criteria(c, criteria)]
    filtered.sort(key=lambda c: c.volume_24h, reverse=True)
    return filtered[:limit] if limit is not None else filtered


def _parse_market(raw_market: dict, tags: list[str], now: datetime) -> MarketCandidate | None:
    condition_id = raw_market.get("conditionId")
    question = raw_market.get("question")
    if not condition_id or not question:
        return None

    try:
        outcomes = json.loads(raw_market.get("outcomes") or "[]")
    except (TypeError, ValueError):
        outcomes = []

    days_to_resolution = None
    end_date_raw = raw_market.get("endDate")
    if end_date_raw:
        try:
            end_date = datetime.fromisoformat(end_date_raw.replace("Z", "+00:00"))
            days_to_resolution = (end_date - now).total_seconds() / 86400
        except ValueError:
            days_to_resolution = None

    return MarketCandidate(
        question=question,
        condition_id=condition_id,
        category=tags,
        volume_24h=float(raw_market.get("volume24hr") or 0.0),
        liquidity=float(raw_market.get("liquidityNum") or 0.0),
        days_to_resolution=days_to_resolution,
        outcome_count=len(outcomes),
    )


def fetch_market_candidates(
    gamma: GammaClient, now: datetime | None = None, page_limit: int = 100, max_markets: int = 2000
) -> list[MarketCandidate]:
    """Pull every active market via GammaClient.get_active_events() and parse
    each into a MarketCandidate, dropping any market missing a condition_id
    or question (shouldn't normally happen, but defensive against partial data)."""
    now = now or datetime.now(timezone.utc)
    candidates: list[MarketCandidate] = []
    for event in gamma.get_active_events(page_limit=page_limit, max_markets=max_markets):
        tags = [tag.get("label") for tag in event.get("tags", []) if tag.get("label")]
        for raw_market in event.get("markets", []):
            candidate = _parse_market(raw_market, tags, now)
            if candidate is not None:
                candidates.append(candidate)
    return candidates


def write_tracked_markets(candidates: list[MarketCandidate], path: str) -> list[dict]:
    """Merge `candidates` into the tracked-markets file at `path`: existing
    entries are kept as-is (deduped by condition_id, existing wins on
    conflict), new ones are appended as {"condition_id", "question"}. Never
    removes anything - if the file has stale/unwanted entries, edit it by
    hand. Returns the full merged list that was written."""
    file_path = Path(path)
    existing: list[dict] = []
    if file_path.exists():
        try:
            existing = json.loads(file_path.read_text())
        except (json.JSONDecodeError, OSError):
            existing = []

    by_id = {
        entry["condition_id"]: entry
        for entry in existing
        if isinstance(entry, dict) and entry.get("condition_id")
    }
    for candidate in candidates:
        by_id.setdefault(
            candidate.condition_id, {"condition_id": candidate.condition_id, "question": candidate.question}
        )

    merged = list(by_id.values())
    file_path.write_text(json.dumps(merged, indent=2))
    return merged


def _build_arg_parser():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--min-volume-24h", type=float, default=0.0)
    parser.add_argument("--min-liquidity", type=float, default=0.0)
    parser.add_argument("--min-days", type=float, default=None, help="Minimum days until resolution")
    parser.add_argument("--max-days", type=float, default=None, help="Maximum days until resolution")
    parser.add_argument(
        "--min-outcomes", type=int, default=None, help="e.g. 3 to require multi-outcome markets"
    )
    parser.add_argument("--max-outcomes", type=int, default=None, help="e.g. 2 to require binary markets")
    parser.add_argument("--category", default=None, help="Case-insensitive match against event tags")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Shortlist size (default 25)")
    parser.add_argument("--output", default=None, help="Write full JSON here instead of stdout")
    parser.add_argument(
        "--write-tracked-markets",
        nargs="?",
        const=settings.tracked_markets_path,
        default=None,
        metavar="PATH",
        help=f"Merge the shortlist's condition_ids into PATH (default {settings.tracked_markets_path})",
    )
    return parser


def main() -> None:
    args = _build_arg_parser().parse_args()
    criteria = ScreenerCriteria(
        min_volume_24h=args.min_volume_24h,
        min_liquidity=args.min_liquidity,
        min_days_to_resolution=args.min_days,
        max_days_to_resolution=args.max_days,
        min_outcome_count=args.min_outcomes,
        max_outcome_count=args.max_outcomes,
        category=args.category,
    )

    gamma = GammaClient()
    try:
        candidates = fetch_market_candidates(gamma)
    finally:
        gamma.close()

    shortlist = rank_markets(candidates, criteria, limit=args.limit)
    output = json.dumps([c.to_dict() for c in shortlist], indent=2)

    if args.output:
        Path(args.output).write_text(output)
        print(f"Wrote {len(shortlist)} market(s) to {args.output}", file=sys.stderr)
    else:
        print(output)

    if args.write_tracked_markets:
        merged = write_tracked_markets(shortlist, args.write_tracked_markets)
        print(
            f"Merged {len(shortlist)} market(s) into {args.write_tracked_markets} "
            f"({len(merged)} tracked total)",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
