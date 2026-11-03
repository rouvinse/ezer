"""
EZER — Shared test fixtures and helpers.

All tests use an in-memory SQLite database so they are:
  • Fast — no file I/O
  • Isolated — each test function gets a fresh DB via the `db_conn` fixture
  • Safe — never touch the real acia.db

Usage:
    pytest tests/ -v
    pytest tests/unit/ -v
    pytest tests/functional/ -v
"""

from __future__ import annotations
import os
import sys
import uuid
import sqlite3
from pathlib import Path
from datetime import datetime

import pytest

# ── Make repo root importable ─────────────────────────────────────────────────
REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

# ── Force SQLite in-memory for all tests ─────────────────────────────────────
os.environ["DB_ENGINE"] = "sqlite"
os.environ["SQLITE_IN_MEMORY"] = "1"      # hint for any env checks

# ── Minimal SQLite schema (mirrors utils/db.py SQLITE_SCHEMA) ────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    company_id   TEXT PRIMARY KEY,
    company_name TEXT NOT NULL,
    email        TEXT UNIQUE,
    plan         TEXT DEFAULT 'starter',
    is_active    INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_keys (
    key_id       TEXT PRIMARY KEY,
    company_id   TEXT NOT NULL,
    company_name TEXT NOT NULL,
    key_hash     TEXT NOT NULL UNIQUE,
    key_prefix   TEXT NOT NULL,
    key_type     TEXT NOT NULL DEFAULT 'live',
    is_active    INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at   TEXT,
    notes        TEXT
);

CREATE TABLE IF NOT EXISTS customers (
    customer_id    TEXT PRIMARY KEY,
    name           TEXT,
    email          TEXT,
    company        TEXT,
    plan           TEXT,
    mrr            REAL,
    signup_date    TEXT,
    country        TEXT,
    health_score   REAL,
    nps_score      INTEGER,
    is_churned     INTEGER DEFAULT 0,
    churn_date     TEXT,
    created_at     TEXT,
    company_id     TEXT DEFAULT 'default'
);

CREATE TABLE IF NOT EXISTS transactions (
    txn_id      TEXT PRIMARY KEY,
    customer_id TEXT,
    amount      REAL,
    currency    TEXT DEFAULT 'USD',
    txn_type    TEXT,
    product_id  TEXT,
    status      TEXT,
    txn_date    TEXT,
    company_id  TEXT DEFAULT 'default'
);

CREATE TABLE IF NOT EXISTS engagement_events (
    event_id         TEXT PRIMARY KEY,
    customer_id      TEXT,
    event_type       TEXT,
    channel          TEXT,
    session_duration INTEGER,
    event_date       TEXT,
    metadata         TEXT,
    company_id       TEXT DEFAULT 'default'
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
    company_id      TEXT DEFAULT 'default'
);

CREATE TABLE IF NOT EXISTS email_log (
    email_id    TEXT PRIMARY KEY,
    customer_id TEXT,
    email_type  TEXT,
    subject     TEXT,
    opened      INTEGER,
    clicked     INTEGER,
    sent_at     TEXT,
    company_id  TEXT DEFAULT 'default'
);

CREATE TABLE IF NOT EXISTS agent_actions (
    action_id   TEXT PRIMARY KEY,
    customer_id TEXT,
    action_type TEXT,
    reason      TEXT,
    priority    INTEGER,
    status      TEXT DEFAULT 'pending',
    outcome     TEXT,
    created_at  TEXT,
    executed_at TEXT,
    decided_by  TEXT,
    company_id  TEXT DEFAULT 'default'
);

CREATE TABLE IF NOT EXISTS ml_predictions (
    prediction_id    TEXT PRIMARY KEY,
    customer_id      TEXT,
    churn_score      REAL,
    conversion_score REAL,
    segment          TEXT,
    ltv_estimate     REAL,
    predicted_at     TEXT,
    company_id       TEXT DEFAULT 'default'
);
"""


def make_db() -> sqlite3.Connection:
    """Open a fresh in-memory SQLite connection with the full schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# ── Pytest fixtures ───────────────────────────────────────────────────────────

@pytest.fixture()
def raw_conn():
    """Raw sqlite3 connection — use where tests need direct SQL control."""
    conn = make_db()
    yield conn
    conn.close()


@pytest.fixture()
def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def insert_customer(conn, company_id="co_a", customer_id=None, **kwargs):
    """Helper — insert a customer row; returns the customer_id."""
    cid = customer_id or str(uuid.uuid4())
    defaults = dict(
        name="Test User",
        email=f"{cid[:8]}@example.com",
        company="Acme",
        plan="starter",
        mrr=100.0,
        signup_date="2025-01-01",
        country="US",
        health_score=80.0,
        nps_score=8,
        is_churned=0,
        created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    defaults.update(kwargs)
    conn.execute(
        "INSERT INTO customers (customer_id, name, email, company, plan, mrr, "
        "signup_date, country, health_score, nps_score, is_churned, created_at, company_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, defaults["name"], defaults["email"], defaults["company"],
         defaults["plan"], defaults["mrr"], defaults["signup_date"],
         defaults["country"], defaults["health_score"], defaults["nps_score"],
         defaults["is_churned"], defaults["created_at"], company_id),
    )
    conn.commit()
    return cid


def insert_action(conn, customer_id, company_id="co_a", action_type="send_retention_email",
                  status="pending", executed_at=None, priority=3):
    aid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO agent_actions "
        "(action_id, customer_id, action_type, reason, priority, status, executed_at, created_at, company_id) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (aid, customer_id, action_type, "test reason", priority, status,
         executed_at, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), company_id),
    )
    conn.commit()
    return aid
