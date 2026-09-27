"""Normalized option chain store shared by the backfill, the forward collector
and the spread backtester.

One SQLite file, two tables:

- option_chain_snapshot: one row per contract per snapshot time. Writes are
  upserts: a re-written (contract, snapshot) replaces the earlier copy.
- daily_close: the companion daily series (VIX and SPX closes), one value per
  symbol per session. The SPX close on an expiry date is the SPXW PM
  settlement value.

Pure stdlib so the Railway service can import it without pandas.

Field conventions:
- root: "SPX" (AM-settled monthlies) or "SPXW" (PM-settled weeklies/dailies).
- expiry / session_date: "YYYY-MM-DD".
- right: "C" or "P".
- snapshot_ms: epoch milliseconds of the snapshot.
- iv: decimal (0.15, not 15).
- sod_oi: SOD Open Interest, i.e. open interest as of the close of T-1.
- vendor_delta / vendor_gamma: as supplied by the data vendor (may be NULL).
- underlying: the derived underlying level at the snapshot.
"""

import sqlite3
from dataclasses import astuple, dataclass, fields
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS option_chain_snapshot (
    root TEXT NOT NULL,
    expiry TEXT NOT NULL,
    strike REAL NOT NULL,
    right TEXT NOT NULL CHECK (right IN ('C', 'P')),
    snapshot_ms INTEGER NOT NULL,
    bid REAL,
    ask REAL,
    iv REAL,
    sod_oi REAL,
    vendor_delta REAL,
    vendor_gamma REAL,
    underlying REAL,
    PRIMARY KEY (root, expiry, strike, right, snapshot_ms)
);
CREATE INDEX IF NOT EXISTS option_chain_snapshot_by_time
    ON option_chain_snapshot (snapshot_ms);
CREATE TABLE IF NOT EXISTS daily_close (
    symbol TEXT NOT NULL,
    session_date TEXT NOT NULL,
    close REAL NOT NULL,
    PRIMARY KEY (symbol, session_date)
);
"""


@dataclass(frozen=True)
class ChainRow:
    root: str
    expiry: str
    strike: float
    right: str
    snapshot_ms: int
    bid: float | None
    ask: float | None
    iv: float | None
    sod_oi: float | None
    vendor_delta: float | None
    vendor_gamma: float | None
    underlying: float | None


CHAIN_COLUMNS = tuple(f.name for f in fields(ChainRow))


def connect(path) -> sqlite3.Connection:
    """Open (creating if needed) a chain store at path."""
    connection = sqlite3.connect(Path(path), timeout=20)
    connection.executescript(SCHEMA)
    return connection


def write_rows(connection: sqlite3.Connection, rows) -> int:
    """Insert chain rows; returns how many were written."""
    values = [astuple(row) for row in rows]
    placeholders = ", ".join("?" for _ in CHAIN_COLUMNS)
    connection.executemany(
        f"INSERT OR REPLACE INTO option_chain_snapshot ({', '.join(CHAIN_COLUMNS)})"
        f" VALUES ({placeholders})", values)
    connection.commit()
    return len(values)


def read_rows(connection: sqlite3.Connection, root=None, start_ms=None, end_ms=None) -> list[ChainRow]:
    """Chain rows ordered by snapshot time, optionally filtered by root and
    an inclusive snapshot_ms window."""
    query = f"SELECT {', '.join(CHAIN_COLUMNS)} FROM option_chain_snapshot WHERE 1 = 1"
    params = []
    if root is not None:
        query += " AND root = ?"
        params.append(root)
    if start_ms is not None:
        query += " AND snapshot_ms >= ?"
        params.append(start_ms)
    if end_ms is not None:
        query += " AND snapshot_ms <= ?"
        params.append(end_ms)
    query += " ORDER BY snapshot_ms, expiry, right, strike"
    return [ChainRow(*row) for row in connection.execute(query, params)]


def write_daily_closes(connection: sqlite3.Connection, symbol: str, closes: dict) -> int:
    """closes: {session_date: close}. Returns how many were written."""
    values = [(symbol, day, float(close)) for day, close in closes.items()]
    connection.executemany(
        "INSERT OR REPLACE INTO daily_close (symbol, session_date, close)"
        " VALUES (?, ?, ?)", values)
    connection.commit()
    return len(values)


def read_daily_closes(connection: sqlite3.Connection, symbol: str) -> dict[str, float]:
    """{session_date: close} for one symbol."""
    return dict(connection.execute(
        "SELECT session_date, close FROM daily_close WHERE symbol = ?"
        " ORDER BY session_date", (symbol,)))
