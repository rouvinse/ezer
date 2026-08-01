"""
ACIA — Database Connection Manager
Single source of truth for all database connections.
Supports both SQLite (development) and PostgreSQL (production).

Configuration via environment variables:
  DB_ENGINE=postgresql          # or 'sqlite' (default)
  DB_HOST=localhost
  DB_PORT=5432
  DB_NAME=acia
  DB_USER=acia_user
  DB_PASSWORD=your_password

  Or use a full connection URL:
  DATABASE_URL=postgresql://user:password@host:5432/acia

Quick start (SQLite — zero config needed):
  Just run the code. SQLite is the default.

Quick start (PostgreSQL local):
  createdb acia
  export DB_ENGINE=postgresql
  export DB_NAME=acia
  export DB_USER=your_pg_username
  python main.py --setup

Quick start (PostgreSQL hosted — Supabase / Railway / RDS):
  export DATABASE_URL=postgresql://user:password@host:5432/acia
  python main.py --setup
"""

from __future__ import annotations
import os
import sqlite3
from pathlib import Path
from contextlib import contextmanager
from typing import Any

# ── Config ────────────────────────────────────────────────────────────────────

ROOT     = Path(__file__).parent.parent
DB_ENGINE  = os.environ.get("DB_ENGINE", "sqlite").lower()
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# SQLite fallback path
SQLITE_PATH = ROOT / "data" / "acia.db"

# PostgreSQL config (used if DB_ENGINE=postgresql and no DATABASE_URL)
PG_HOST     = os.environ.get("DB_HOST",     "localhost")
PG_PORT     = int(os.environ.get("DB_PORT", "5432"))
PG_NAME     = os.environ.get("DB_NAME",     "acia")
PG_USER     = os.environ.get("DB_USER",     "")
PG_PASSWORD = os.environ.get("DB_PASSWORD", "")

# Detect engine from DATABASE_URL if provided
if DATABASE_URL.startswith("postgresql") or DATABASE_URL.startswith("postgres"):
    DB_ENGINE = "postgresql"


# ── Connection wrappers ────────────────────────────────────────────────────────

class ACIAConnection:
    """
    Unified connection wrapper.
    Provides the same interface whether using SQLite or PostgreSQL,
    so all callers work identically with either backend.
    """

    def __init__(self, conn, engine: str):
        self._conn   = conn
        self._engine = engine

    def execute(self, sql: str, params: Any = None):
        """Execute a single SQL statement."""
        sql = self._adapt_sql(sql)
        cur = self._conn.cursor()
        if params is None:
            cur.execute(sql)
        else:
            cur.execute(sql, params)
        return cur

    def executemany(self, sql: str, params_seq):
        """Execute a SQL statement with multiple parameter sets."""
        sql = self._adapt_sql(sql)
        cur = self._conn.cursor()
        cur.executemany(sql, params_seq)
        return cur

    def executescript(self, script: str):
        """Execute a multi-statement SQL script."""
        if self._engine == "sqlite":
            self._conn.executescript(script)
        else:
            # PostgreSQL doesn't have executescript — split on ; and run each
            statements = [s.strip() for s in script.split(";") if s.strip()]
            cur = self._conn.cursor()
            for stmt in statements:
                try:
                    cur.execute(stmt)
                except Exception:
                    pass  # Skip errors on IF NOT EXISTS statements

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()

    def cursor(self):
        return self._conn.cursor()

    def _adapt_sql(self, sql: str) -> str:
        """
        Adapt SQLite-style SQL to PostgreSQL where needed.
        SQLite uses ? placeholders. PostgreSQL uses %s.
        """
        if self._engine == "postgresql":
            sql = sql.replace("?", "%s")
            # SQLite uses INTEGER PRIMARY KEY autoincrement differently
            # but since we use UUIDs, this isn't an issue for ACIA
        return sql

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            self.commit()
        else:
            self._conn.rollback()
        self.close()


def get_connection() -> ACIAConnection:
    """
    Get a database connection.
    Returns ACIAConnection wrapping either SQLite or PostgreSQL.
    """
    if DB_ENGINE == "postgresql":
        return _get_pg_connection()
    return _get_sqlite_connection()


def _get_sqlite_connection() -> ACIAConnection:
    """Open a SQLite connection."""
    SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(SQLITE_PATH))
    conn.row_factory = sqlite3.Row
    return ACIAConnection(conn, "sqlite")


def _get_pg_connection() -> ACIAConnection:
    """Open a PostgreSQL connection."""
    try:
        import psycopg2
        import psycopg2.extras
    except ImportError:
        raise ImportError(
            "psycopg2 not installed. Run: pip install psycopg2-binary"
        )

    if DATABASE_URL:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    else:
        conn = psycopg2.connect(
            host=PG_HOST,
            port=PG_PORT,
            dbname=PG_NAME,
            user=PG_USER,
            password=PG_PASSWORD,
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
    conn.autocommit = False
    return ACIAConnection(conn, "postgresql")


@contextmanager
def db():
    """
    Context manager for safe connection handling.

    Usage:
        with db() as conn:
            conn.execute("INSERT INTO ...")
        # auto-commits on success, rolls back on exception, always closes

    """
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn._conn.rollback()
        raise
    finally:
        conn.close()


# ── pandas helper ─────────────────────────────────────────────────────────────

def read_sql(query: str, params=None) -> "pd.DataFrame":
    """
    Execute a SELECT query and return a pandas DataFrame.
    Works with both SQLite and PostgreSQL.
    Uses cursor-based fetching to avoid pandas connection warnings.
    """
    import pandas as pd
    conn = get_connection()
    try:
        adapted = conn._adapt_sql(query)
        cur = conn._conn.cursor()
        if params:
            cur.execute(adapted, params)
        else:
            cur.execute(adapted)
        rows = cur.fetchall()
        if not rows:
            # Return empty DataFrame with correct columns
            col_names = [d[0] for d in cur.description] if cur.description else []
            return pd.DataFrame(columns=col_names)
        # Build DataFrame from rows
        col_names = [d[0] for d in cur.description]
        return pd.DataFrame([dict(zip(col_names, row)) for row in rows])
    finally:
        conn.close()


# ── Schema ────────────────────────────────────────────────────────────────────

# PostgreSQL-compatible schema
# Differences from SQLite version:
#   - TEXT PRIMARY KEY → VARCHAR(36) PRIMARY KEY (UUID strings)
#   - No INTEGER PRIMARY KEY autoincrement (we use UUIDs everywhere)
#   - REAL → DOUBLE PRECISION
#   - Added IF NOT EXISTS everywhere

PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    customer_id     VARCHAR(36) PRIMARY KEY,
    name            TEXT,
    email           TEXT UNIQUE,
    company         TEXT,
    industry        TEXT,
    plan            TEXT,
    mrr             DOUBLE PRECISION,
    signup_date     TEXT,
    country         TEXT,
    employee_count  INTEGER,
    health_score    DOUBLE PRECISION,
    nps_score       INTEGER,
    is_churned      INTEGER,
    churn_date      TEXT,
    created_at      TEXT
);

CREATE TABLE IF NOT EXISTS transactions (
    txn_id          VARCHAR(36) PRIMARY KEY,
    customer_id     VARCHAR(36),
    amount          DOUBLE PRECISION,
    currency        TEXT DEFAULT 'USD',
    txn_type        TEXT,
    product_id      TEXT,
    status          TEXT,
    txn_date        TEXT,
    company_id      TEXT DEFAULT 'default',
    FOREIGN KEY(customer_id) REFERENCES customers(customer_id)
);

CREATE TABLE IF NOT EXISTS engagement_events (
    event_id        VARCHAR(36) PRIMARY KEY,
    customer_id     VARCHAR(36),
    event_type      TEXT,
    channel         TEXT,
    session_duration INTEGER,
    event_date      TEXT,
    metadata        TEXT,
    company_id      TEXT DEFAULT 'default',
    FOREIGN KEY(customer_id) REFERENCES customers(customer_id)
);

CREATE TABLE IF NOT EXISTS support_tickets (
    ticket_id       VARCHAR(36) PRIMARY KEY,
    customer_id     VARCHAR(36),
    topic           TEXT,
    status          TEXT,
    priority        TEXT,
    sentiment_score DOUBLE PRECISION,
    opened_at       TEXT,
    resolved_at     TEXT,
    company_id      TEXT DEFAULT 'default',
    FOREIGN KEY(customer_id) REFERENCES customers(customer_id)
);

CREATE TABLE IF NOT EXISTS email_log (
    email_id        VARCHAR(36) PRIMARY KEY,
    customer_id     VARCHAR(36),
    email_type      TEXT,
    subject         TEXT,
    opened          INTEGER,
    clicked         INTEGER,
    sent_at         TEXT,
    company_id      TEXT DEFAULT 'default',
    FOREIGN KEY(customer_id) REFERENCES customers(customer_id)
);

CREATE TABLE IF NOT EXISTS agent_actions (
    action_id       VARCHAR(36) PRIMARY KEY,
    customer_id     VARCHAR(36),
    action_type     TEXT,
    reason          TEXT,
    priority        INTEGER,
    status          TEXT DEFAULT 'pending',
    outcome         TEXT,
    created_at      TEXT,
    executed_at     TEXT,
    company_id      TEXT DEFAULT 'default',
    FOREIGN KEY(customer_id) REFERENCES customers(customer_id)
);

CREATE TABLE IF NOT EXISTS ml_predictions (
    prediction_id   VARCHAR(36) PRIMARY KEY,
    customer_id     VARCHAR(36),
    churn_score     DOUBLE PRECISION,
    conversion_score DOUBLE PRECISION,
    segment         TEXT,
    ltv_estimate    DOUBLE PRECISION,
    predicted_at    TEXT,
    company_id      TEXT DEFAULT 'default',
    FOREIGN KEY(customer_id) REFERENCES customers(customer_id)
);
"""

# SQLite schema (original)
SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    customer_id     TEXT PRIMARY KEY,
    name            TEXT,
    email           TEXT,
    company         TEXT,
    industry        TEXT,
    plan            TEXT,
    mrr             REAL,
    signup_date     TEXT,
    country         TEXT,
    employee_count  INTEGER,
    health_score    REAL,
    nps_score       INTEGER,
    is_churned      INTEGER,
    churn_date      TEXT,
    created_at      TEXT,
    company_id      TEXT DEFAULT 'default'
);

CREATE TABLE IF NOT EXISTS transactions (
    txn_id          TEXT PRIMARY KEY,
    customer_id     TEXT,
    amount          REAL,
    currency        TEXT DEFAULT 'USD',
    txn_type        TEXT,
    product_id      TEXT,
    status          TEXT,
    txn_date        TEXT,
    company_id      VARCHAR(36) DEFAULT 'default',
    FOREIGN KEY(customer_id) REFERENCES customers(customer_id)
);

CREATE TABLE IF NOT EXISTS engagement_events (
    event_id        TEXT PRIMARY KEY,
    customer_id     TEXT,
    event_type      TEXT,
    channel         TEXT,
    session_duration INTEGER,
    event_date      TEXT,
    metadata        TEXT,
    company_id      VARCHAR(36) DEFAULT 'default',
    FOREIGN KEY(customer_id) REFERENCES customers(customer_id)
);

CREATE TABLE IF NOT EXISTS support_tickets (
    ticket_id       TEXT PRIMARY KEY,
    customer_id     TEXT,
    topic           TEXT,
    status          TEXT,
    priority        TEXT,
    sentiment_score REAL,
    opened_at       TEXT,
    resolved_at     TEXT,
    company_id      VARCHAR(36) DEFAULT 'default',
    FOREIGN KEY(customer_id) REFERENCES customers(customer_id)
);

CREATE TABLE IF NOT EXISTS email_log (
    email_id        TEXT PRIMARY KEY,
    customer_id     TEXT,
    email_type      TEXT,
    subject         TEXT,
    opened          INTEGER,
    clicked         INTEGER,
    sent_at         TEXT,
    company_id      VARCHAR(36) DEFAULT 'default',
    FOREIGN KEY(customer_id) REFERENCES customers(customer_id)
);

CREATE TABLE IF NOT EXISTS agent_actions (
    action_id       TEXT PRIMARY KEY,
    customer_id     TEXT,
    action_type     TEXT,
    reason          TEXT,
    priority        INTEGER,
    status          TEXT DEFAULT 'pending',
    outcome         TEXT,
    created_at      TEXT,
    executed_at     TEXT,
    company_id      VARCHAR(36) DEFAULT 'default',
    FOREIGN KEY(customer_id) REFERENCES customers(customer_id)
);

CREATE TABLE IF NOT EXISTS ml_predictions (
    prediction_id   TEXT PRIMARY KEY,
    customer_id     TEXT,
    churn_score     REAL,
    conversion_score REAL,
    segment         TEXT,
    ltv_estimate    REAL,
    predicted_at    TEXT,
    company_id      TEXT DEFAULT 'default',
    FOREIGN KEY(customer_id) REFERENCES customers(customer_id)
);
"""


def get_schema() -> str:
    """Return the appropriate schema for the current engine."""
    return PG_SCHEMA if DB_ENGINE == "postgresql" else SQLITE_SCHEMA


# ── Status checker ────────────────────────────────────────────────────────────

def print_db_status():
    """Print current database configuration and connection status."""
    print(f"\n── Database Configuration ───────────────────────────")
    print(f"  Engine : {DB_ENGINE.upper()}")

    if DB_ENGINE == "postgresql":
        if DATABASE_URL:
            # Mask password in URL for display
            display_url = DATABASE_URL
            if "@" in DATABASE_URL:
                parts        = DATABASE_URL.split("@")
                creds        = parts[0].split("://")[1]
                masked       = creds.split(":")[0] + ":****"
                display_url  = DATABASE_URL.split("://")[0] + "://" + masked + "@" + parts[1]
            print(f"  URL    : {display_url}")
        else:
            print(f"  Host   : {PG_HOST}:{PG_PORT}")
            print(f"  DB     : {PG_NAME}")
            print(f"  User   : {PG_USER or '(not set)'}")

    else:
        print(f"  File   : {SQLITE_PATH}")

    # Test connection
    try:
        conn = get_connection()
        result = conn.execute("SELECT COUNT(*) FROM customers")
        row  = result.fetchone()
        count = row[0] if row else 0
        conn.close()
        print(f"  Status : ✅ connected ({count} customers)")
    except Exception as e:
        print(f"  Status : ❌ connection failed — {e}")

    print(f"────────────────────────────────────────────────────\n")


if __name__ == "__main__":
    print_db_status()
