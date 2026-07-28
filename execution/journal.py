"""Persistent log of strategy activity (signals + market-making quotes) and
the risk-gated decisions that came out of them - what the CLI (scripts/cli.py)
reads to answer "what has the bot been doing" from a separate process,
independent of whether main.py is still running.

Kept separate from data.store.DataStore: that module owns market data (order
books, metadata); this owns execution-side history. Both can point at the
same db_path - SQLite handles multiple connections to one file fine for this
single-writer (main.py), occasional-reader (the CLI) pattern.
"""

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from execution.models import OrderDecision
from signals.base import Signal
from signals.market_making.models import QuotePair

SCHEMA = """
CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    strategy TEXT NOT NULL,
    condition_id TEXT,
    kind TEXT NOT NULL,
    token_id TEXT,
    side TEXT,
    edge_estimate REAL,
    confidence REAL,
    bid_price REAL,
    bid_size REAL,
    ask_price REAL,
    ask_size REAL
);
CREATE INDEX IF NOT EXISTS idx_activity_log_timestamp ON activity_log (timestamp);

CREATE TABLE IF NOT EXISTS decisions_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    strategy TEXT NOT NULL,
    condition_id TEXT,
    token_id TEXT,
    side TEXT,
    price REAL,
    size REAL,
    status TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    order_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_log_timestamp ON decisions_log (timestamp);
"""


@dataclass(frozen=True)
class ActivityRecord:
    timestamp: datetime
    strategy: str
    condition_id: str | None
    kind: str  # "signal" | "quote"
    token_id: str | None
    side: str | None
    edge_estimate: float | None
    confidence: float | None
    bid_price: float | None
    bid_size: float | None
    ask_price: float | None
    ask_size: float | None


@dataclass(frozen=True)
class DecisionRecord:
    timestamp: datetime
    strategy: str
    condition_id: str | None
    token_id: str | None
    side: str | None
    price: float | None
    size: float | None
    status: str
    reasons: list[str] = field(default_factory=list)
    order_id: str | None = None


class DecisionJournal:
    def __init__(self, db_path: str | Path):
        parent = Path(db_path).parent
        if str(parent):
            parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def record_signal(
        self, strategy: str, condition_id: str | None, signal: Signal, timestamp: datetime
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO activity_log
                    (timestamp, strategy, condition_id, kind, token_id, side, edge_estimate, confidence)
                VALUES (?, ?, ?, 'signal', ?, ?, ?, ?)
                """,
                (
                    timestamp.isoformat(),
                    strategy,
                    condition_id,
                    signal.token_id,
                    signal.side.value,
                    signal.edge_estimate,
                    signal.confidence,
                ),
            )
            self._conn.commit()

    def record_quote(
        self, strategy: str, condition_id: str | None, quote_pair: QuotePair, timestamp: datetime
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO activity_log
                    (timestamp, strategy, condition_id, kind, token_id,
                     bid_price, bid_size, ask_price, ask_size)
                VALUES (?, ?, ?, 'quote', ?, ?, ?, ?, ?)
                """,
                (
                    timestamp.isoformat(),
                    strategy,
                    condition_id,
                    quote_pair.token_id,
                    quote_pair.bid.price if quote_pair.bid else None,
                    quote_pair.bid.size if quote_pair.bid else None,
                    quote_pair.ask.price if quote_pair.ask else None,
                    quote_pair.ask.size if quote_pair.ask else None,
                ),
            )
            self._conn.commit()

    def record_decision(
        self,
        strategy: str,
        condition_id: str | None,
        decision: OrderDecision,
        timestamp: datetime,
    ) -> None:
        intent = decision.intent
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO decisions_log
                    (timestamp, strategy, condition_id, token_id, side, price, size,
                     status, reasons_json, order_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp.isoformat(),
                    strategy,
                    condition_id,
                    intent.token_id if intent else None,
                    intent.side.value if intent else None,
                    intent.price if intent else None,
                    intent.size if intent else None,
                    decision.status.value,
                    json.dumps(decision.reasons),
                    decision.order_id,
                ),
            )
            self._conn.commit()

    def recent_activity(self, limit: int = 20) -> list[ActivityRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM activity_log ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_activity(row) for row in rows]

    def recent_decisions(self, limit: int = 20) -> list[DecisionRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM decisions_log ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_decision(row) for row in rows]

    def all_decisions(self) -> list[DecisionRecord]:
        """Every decision ever logged, oldest first - what the CLI replays
        through a fresh backtest.portfolio.Portfolio to compute current
        (simulated) positions and P&L.

        Unbounded - decisions_log has no pruning (unlike
        data.store.DataStore's order_book_snapshots), so this can become a
        very slow full-table scan once the bot has run a while (hit 9.5M
        rows/multi-minute query in real operation, mostly rejected
        market_making quotes logged on every book tick). Prefer
        decisions_since() for interactive use."""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM decisions_log ORDER BY timestamp ASC").fetchall()
        return [_row_to_decision(row) for row in rows]

    def decisions_since(self, start: datetime) -> list[DecisionRecord]:
        """Same as all_decisions() but bounded to timestamp >= start, using
        idx_decisions_log_timestamp - the practical way to query once
        decisions_log has grown large. Correctness caveat: a position opened
        before `start` and still open won't have its cost basis included, so
        this can misstate P&L for positions that have been open longer than
        the window - fine for game markets (resolve in hours) but not a
        substitute for all_decisions() if you need a fully correct replay."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM decisions_log WHERE timestamp >= ? ORDER BY timestamp ASC",
                (start.isoformat(),),
            ).fetchall()
        return [_row_to_decision(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _row_to_activity(row: sqlite3.Row) -> ActivityRecord:
    return ActivityRecord(
        timestamp=datetime.fromisoformat(row["timestamp"]),
        strategy=row["strategy"],
        condition_id=row["condition_id"],
        kind=row["kind"],
        token_id=row["token_id"],
        side=row["side"],
        edge_estimate=row["edge_estimate"],
        confidence=row["confidence"],
        bid_price=row["bid_price"],
        bid_size=row["bid_size"],
        ask_price=row["ask_price"],
        ask_size=row["ask_size"],
    )


def _row_to_decision(row: sqlite3.Row) -> DecisionRecord:
    return DecisionRecord(
        timestamp=datetime.fromisoformat(row["timestamp"]),
        strategy=row["strategy"],
        condition_id=row["condition_id"],
        token_id=row["token_id"],
        side=row["side"],
        price=row["price"],
        size=row["size"],
        status=row["status"],
        reasons=json.loads(row["reasons_json"]),
        order_id=row["order_id"],
    )
