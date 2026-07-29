"""SQLite persistence for order book snapshots and market metadata, used later
by backtest/ to replay history. Kept to stdlib sqlite3 - no extra dependency,
and the data volumes here (order book snapshots for a handful of markets) don't
need anything heavier.
"""

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from data.models import MarketMetadata, OrderBook, PriceLevel

SCHEMA = """
CREATE TABLE IF NOT EXISTS order_book_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id TEXT NOT NULL,
    condition_id TEXT,
    exchange_timestamp TEXT,
    received_at TEXT NOT NULL,
    book_hash TEXT,
    bids_json TEXT NOT NULL,
    asks_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_order_book_snapshots_token_received
    ON order_book_snapshots (token_id, received_at);
-- Separate from the composite index above: that one can't be used for a
-- bare "WHERE received_at < ?" predicate (received_at isn't its leftmost
-- column), which scripts/checkpoint_and_prune.py needs for pruning old
-- snapshots - without this, that DELETE is a full table scan on a
-- multi-GB table.
CREATE INDEX IF NOT EXISTS idx_order_book_snapshots_received_at
    ON order_book_snapshots (received_at);

CREATE TABLE IF NOT EXISTS market_metadata (
    condition_id TEXT PRIMARY KEY,
    question_id TEXT,
    question TEXT,
    description TEXT,
    resolution_source TEXT,
    category TEXT,
    end_date TEXT,
    active INTEGER,
    closed INTEGER,
    outcomes_json TEXT NOT NULL,
    outcome_prices_json TEXT NOT NULL,
    token_ids_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
"""


def _levels_to_json(levels: list[PriceLevel]) -> str:
    return json.dumps([{"price": level.price, "size": level.size} for level in levels])


class DataStore:
    """Thread-safe SQLite store. Safe to share across the asyncio ingestion loop
    (via asyncio.to_thread) and any synchronous backtest code that reads later."""

    def __init__(self, db_path: str | Path):
        parent = Path(db_path).parent
        if str(parent):
            parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=60.0)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            # WAL lets readers (e.g. backtest scripts) run concurrently with
            # the ingestion loop's frequent writes instead of hitting
            # "database is locked" under the default rollback journal, which
            # takes an exclusive lock for the duration of every write.
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def save_order_book(self, book: OrderBook) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO order_book_snapshots
                    (token_id, condition_id, exchange_timestamp, received_at,
                     book_hash, bids_json, asks_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    book.token_id,
                    book.condition_id,
                    book.exchange_timestamp.isoformat() if book.exchange_timestamp else None,
                    book.received_at.isoformat(),
                    book.book_hash,
                    _levels_to_json(book.bids),
                    _levels_to_json(book.asks),
                ),
            )
            self._conn.commit()

    def save_market_metadata(self, market: MarketMetadata) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO market_metadata
                    (condition_id, question_id, question, description, resolution_source,
                     category, end_date, active, closed, outcomes_json, outcome_prices_json,
                     token_ids_json, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(condition_id) DO UPDATE SET
                    question_id=excluded.question_id,
                    question=excluded.question,
                    description=excluded.description,
                    resolution_source=excluded.resolution_source,
                    category=excluded.category,
                    end_date=excluded.end_date,
                    active=excluded.active,
                    closed=excluded.closed,
                    outcomes_json=excluded.outcomes_json,
                    outcome_prices_json=excluded.outcome_prices_json,
                    token_ids_json=excluded.token_ids_json,
                    fetched_at=excluded.fetched_at
                """,
                (
                    market.condition_id,
                    market.question_id,
                    market.question,
                    market.description,
                    market.resolution_source,
                    market.category,
                    market.end_date.isoformat() if market.end_date else None,
                    market.active,
                    market.closed,
                    json.dumps(market.outcomes),
                    json.dumps(market.outcome_prices),
                    json.dumps(market.token_ids),
                    market.fetched_at.isoformat(),
                ),
            )
            self._conn.commit()

    def count_order_book_snapshots(self) -> int:
        with self._lock:
            cursor = self._conn.execute("SELECT COUNT(*) FROM order_book_snapshots")
            return cursor.fetchone()[0]

    def get_market_metadata(self, condition_id: str) -> MarketMetadata | None:
        """The current (latest-known) metadata for one market, or None if
        it's never been stored."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM market_metadata WHERE condition_id = ?", (condition_id,)
            ).fetchone()
        return _row_to_market_metadata(row) if row is not None else None

    def list_order_book_snapshots(
        self,
        token_ids: list[str],
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[OrderBook]:
        """Every snapshot for the given token_ids within [start, end] (either
        bound optional), ordered by received_at ascending - a single merged
        chronological stream across all the given tokens, which is what
        backtest/ needs to replay history in order."""
        if not token_ids:
            return []
        placeholders = ",".join("?" for _ in token_ids)
        query = f"SELECT * FROM order_book_snapshots WHERE token_id IN ({placeholders})"
        params: list[str] = list(token_ids)
        if start is not None:
            query += " AND received_at >= ?"
            params.append(start.isoformat())
        if end is not None:
            query += " AND received_at <= ?"
            params.append(end.isoformat())
        query += " ORDER BY received_at ASC"

        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [_row_to_order_book(row) for row in rows]

    def get_latest_order_book(self, token_id: str) -> OrderBook | None:
        """The most recent snapshot for one token, or None if none stored -
        used for mark-to-market pricing (e.g. scripts/cli.py) rather than
        replaying the whole history."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM order_book_snapshots WHERE token_id = ? "
                "ORDER BY received_at DESC LIMIT 1",
                (token_id,),
            ).fetchone()
        return _row_to_order_book(row) if row is not None else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _row_to_market_metadata(row: sqlite3.Row) -> MarketMetadata:
    return MarketMetadata(
        condition_id=row["condition_id"],
        question_id=row["question_id"],
        question=row["question"],
        description=row["description"],
        resolution_source=row["resolution_source"],
        category=row["category"],
        end_date=datetime.fromisoformat(row["end_date"]) if row["end_date"] else None,
        active=bool(row["active"]) if row["active"] is not None else None,
        closed=bool(row["closed"]) if row["closed"] is not None else None,
        outcomes=json.loads(row["outcomes_json"]),
        outcome_prices=json.loads(row["outcome_prices_json"]),
        token_ids=json.loads(row["token_ids_json"]),
        fetched_at=datetime.fromisoformat(row["fetched_at"]),
    )


def _row_to_order_book(row: sqlite3.Row) -> OrderBook:
    return OrderBook(
        token_id=row["token_id"],
        condition_id=row["condition_id"],
        bids=[PriceLevel(level["price"], level["size"]) for level in json.loads(row["bids_json"])],
        asks=[PriceLevel(level["price"], level["size"]) for level in json.loads(row["asks_json"])],
        exchange_timestamp=(
            datetime.fromisoformat(row["exchange_timestamp"]) if row["exchange_timestamp"] else None
        ),
        book_hash=row["book_hash"],
        received_at=datetime.fromisoformat(row["received_at"]),
    )
