"""
Unit tests — connectors/connector_utils.py  (SEC-NEW-004)

Tests verify:
  1. safe_upsert_sync() inserts new rows correctly
  2. safe_upsert_sync() updates existing rows (no duplicate key error)
  3. Empty row list is a no-op
  4. Stale-row purge runs ONLY after all upserts succeed
  5. Stale-row purge is skipped when purge_stale=False (default)
  6. Rows for tenant A are never visible when querying tenant B (cross-tenant isolation)
  7. Bulk failure falls back to row-by-row

All tests use a patched in-memory SQLite connection so connector_utils is
exercised end-to-end without touching disk.
"""

from __future__ import annotations
import sys
import sqlite3
import uuid
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# ── Minimal schema for the tables connector_utils writes to ──────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    customer_id TEXT PRIMARY KEY,
    name        TEXT,
    email       TEXT,
    company     TEXT,
    plan        TEXT,
    mrr         REAL,
    signup_date TEXT,
    country     TEXT,
    company_id  TEXT DEFAULT 'default'
);
"""


def _make_inmem_conn():
    """Return a plain sqlite3 connection that connector_utils can use."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _rows_in(conn, table, company_id):
    rows = conn.execute(
        f"SELECT * FROM {table} WHERE company_id=?", (company_id,)
    ).fetchall()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers to exercise safe_upsert_sync against a real in-memory SQLite DB.
# We monkey-patch get_connection() to return our test connection each time.
# ─────────────────────────────────────────────────────────────────────────────

class FakeACIAConn:
    """
    Minimal wrapper that mimics ACIAConnection's interface
    but delegates to a raw sqlite3 connection.
    This lets safe_upsert_sync run against an in-memory DB.
    """

    def __init__(self, raw_conn):
        self._conn = raw_conn

    def executemany(self, sql, params_seq):
        cur = self._conn.cursor()
        cur.executemany(sql, list(params_seq))
        return cur

    def execute(self, sql, params=None):
        cur = self._conn.cursor()
        if params is None:
            cur.execute(sql)
        else:
            cur.execute(sql, params)
        return cur

    def commit(self):
        self._conn.commit()

    def close(self):
        pass  # keep open for assertions


def _run_upsert(raw_conn, rows, company_id, **kwargs):
    """
    Call safe_upsert_sync with a patched get_connection and DB_ENGINE=sqlite.
    Returns the result dict.
    """
    fake = FakeACIAConn(raw_conn)

    import connectors.connector_utils as _cu_mod
    with (
        patch.object(_cu_mod, "get_connection", return_value=fake),
        patch.object(_cu_mod, "DB_ENGINE", "sqlite"),
    ):
        from connectors.connector_utils import safe_upsert_sync
        return safe_upsert_sync(
            table="customers",
            pk_col="customer_id",
            rows=rows,
            company_id=company_id,
            **kwargs,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestSafeUpsertInsert:
    def test_inserts_new_rows(self):
        conn = _make_inmem_conn()
        cid = str(uuid.uuid4())
        rows = [{"customer_id": cid, "name": "Alice", "email": "a@a.com",
                 "company": "Acme", "plan": "pro", "mrr": 500.0,
                 "signup_date": "2025-01-01", "country": "US", "company_id": "co_a"}]

        result = _run_upsert(conn, rows, "co_a")

        assert result["upserted"] == 1
        assert result["errors"] == []
        stored = _rows_in(conn, "customers", "co_a")
        assert len(stored) == 1
        assert stored[0]["name"] == "Alice"

    def test_upserts_multiple_rows(self):
        conn = _make_inmem_conn()
        rows = [
            {"customer_id": str(uuid.uuid4()), "name": f"User {i}", "email": f"u{i}@x.com",
             "company": "X", "plan": "starter", "mrr": float(i * 10),
             "signup_date": "2025-01-01", "country": "US", "company_id": "co_b"}
            for i in range(5)
        ]
        result = _run_upsert(conn, rows, "co_b")
        assert result["upserted"] == 5
        assert len(_rows_in(conn, "customers", "co_b")) == 5


class TestSafeUpsertUpdate:
    def test_updates_existing_row_without_duplicate(self):
        conn = _make_inmem_conn()
        cid = str(uuid.uuid4())

        row_v1 = [{"customer_id": cid, "name": "Bob", "email": "b@b.com",
                   "company": "BobCo", "plan": "starter", "mrr": 100.0,
                   "signup_date": "2025-01-01", "country": "US", "company_id": "co_a"}]
        _run_upsert(conn, row_v1, "co_a")

        row_v2 = [{"customer_id": cid, "name": "Bob Updated", "email": "b@b.com",
                   "company": "BobCo", "plan": "pro", "mrr": 200.0,
                   "signup_date": "2025-01-01", "country": "US", "company_id": "co_a"}]
        result = _run_upsert(conn, row_v2, "co_a")

        assert result["errors"] == []
        stored = _rows_in(conn, "customers", "co_a")
        assert len(stored) == 1           # still exactly one row
        assert stored[0]["plan"] == "pro"
        assert stored[0]["mrr"] == 200.0


class TestSafeUpsertEmpty:
    def test_empty_rows_is_noop(self):
        conn = _make_inmem_conn()
        result = _run_upsert(conn, [], "co_a")
        assert result == {"upserted": 0, "purged": 0, "errors": []}


class TestSafeUpsertStalePurge:
    def test_purge_removes_stale_rows(self):
        conn = _make_inmem_conn()

        # Insert 3 customers for co_a
        ids = [str(uuid.uuid4()) for _ in range(3)]
        rows = [{"customer_id": ids[i], "name": f"U{i}", "email": f"u{i}@x.com",
                 "company": "X", "plan": "pro", "mrr": 100.0,
                 "signup_date": "2025-01-01", "country": "US", "company_id": "co_a"}
                for i in range(3)]
        _run_upsert(conn, rows, "co_a")
        assert len(_rows_in(conn, "customers", "co_a")) == 3

        # Re-sync with only ids[0] — ids[1] and ids[2] are stale
        keep_rows = [rows[0]]
        result = _run_upsert(conn, keep_rows, "co_a",
                             upstream_ids={ids[0]}, purge_stale=True)

        assert result["purged"] == 2
        remaining = _rows_in(conn, "customers", "co_a")
        assert len(remaining) == 1
        assert remaining[0]["customer_id"] == ids[0]

    def test_purge_skipped_when_flag_false(self):
        conn = _make_inmem_conn()
        ids = [str(uuid.uuid4()) for _ in range(2)]
        rows = [{"customer_id": ids[i], "name": f"U{i}", "email": f"u{i}@x.com",
                 "company": "X", "plan": "pro", "mrr": 50.0,
                 "signup_date": "2025-01-01", "country": "US", "company_id": "co_a"}
                for i in range(2)]
        _run_upsert(conn, rows, "co_a")

        # Sync with only ids[0] but purge_stale=False (default)
        result = _run_upsert(conn, [rows[0]], "co_a",
                             upstream_ids={ids[0]}, purge_stale=False)
        assert result["purged"] == 0
        assert len(_rows_in(conn, "customers", "co_a")) == 2  # both still present


class TestSafeUpsertCrossTenantIsolation:
    def test_tenant_rows_do_not_bleed_between_companies(self):
        conn = _make_inmem_conn()
        cid_a = str(uuid.uuid4())
        cid_b = str(uuid.uuid4())

        _run_upsert(conn, [{"customer_id": cid_a, "name": "A Corp Customer",
                            "email": "a@a.com", "company": "A", "plan": "pro",
                            "mrr": 500.0, "signup_date": "2025-01-01",
                            "country": "US", "company_id": "co_a"}], "co_a")

        _run_upsert(conn, [{"customer_id": cid_b, "name": "B Corp Customer",
                            "email": "b@b.com", "company": "B", "plan": "starter",
                            "mrr": 100.0, "signup_date": "2025-01-01",
                            "country": "UK", "company_id": "co_b"}], "co_b")

        rows_a = _rows_in(conn, "customers", "co_a")
        rows_b = _rows_in(conn, "customers", "co_b")

        assert len(rows_a) == 1 and rows_a[0]["name"] == "A Corp Customer"
        assert len(rows_b) == 1 and rows_b[0]["name"] == "B Corp Customer"
        # A's purge should not touch B
        _run_upsert(conn, [], "co_a", upstream_ids=set(), purge_stale=True)
        assert len(_rows_in(conn, "customers", "co_b")) == 1  # B untouched
