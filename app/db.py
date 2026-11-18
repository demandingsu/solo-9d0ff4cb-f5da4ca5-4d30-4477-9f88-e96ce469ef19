"""SQLite persistence layer.

Consistency model
-----------------
* Every write (snapshot replace, license change, activate/deactivate,
  package creation) runs inside ONE ``BEGIN IMMEDIATE`` transaction and
  bumps ``meta.version`` in the same commit.  A failed write rolls back
  completely, so readers keep seeing the previous committed version.
* Every query runs inside ONE read transaction.  With WAL mode a read
  transaction sees a stable snapshot of the database as of its first
  statement, so a concurrent commit can never leak half-applied data
  into an answer: a query always uses exactly one committed version.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO meta (id, version) VALUES (1, 0);

CREATE TABLE IF NOT EXISTS packages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    license_start TEXT NOT NULL,          -- ISO date, inclusive
    license_end   TEXT NOT NULL,          -- ISO date, inclusive
    active        INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS coverage (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id     INTEGER NOT NULL REFERENCES packages(id) ON DELETE CASCADE,
    issn           TEXT NOT NULL,          -- normalized XXXX-XXXX, upper case
    start_date     TEXT NOT NULL,          -- ISO date, inclusive
    end_date       TEXT NOT NULL,          -- ISO date, inclusive
    embargo_months INTEGER NOT NULL CHECK (embargo_months >= 0)
);
CREATE INDEX IF NOT EXISTS idx_coverage_issn ON coverage(issn);
CREATE INDEX IF NOT EXISTS idx_coverage_pkg  ON coverage(package_id);
"""


def db_path() -> str:
    return os.environ.get("DB_PATH", "library.db")


def connect() -> sqlite3.Connection:
    # isolation_level=None -> autocommit; transactions are opened
    # explicitly so read and write semantics stay under our control.
    conn = sqlite3.connect(db_path(), isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def write_tx() -> Iterator[sqlite3.Connection]:
    """Exclusive write transaction; rolls back on any exception."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


@contextmanager
def read_tx() -> Iterator[sqlite3.Connection]:
    """Single read transaction -> one consistent committed snapshot."""
    conn = connect()
    try:
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def bump_version(conn: sqlite3.Connection) -> int:
    """Advance the global data version; must run inside a write tx."""
    row = conn.execute(
        "UPDATE meta SET version = version + 1 WHERE id = 1 RETURNING version"
    ).fetchone()
    return int(row["version"])


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT version FROM meta WHERE id = 1").fetchone()
    return int(row["version"])
