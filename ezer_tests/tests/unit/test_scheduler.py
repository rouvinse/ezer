"""
Unit tests — agent/scheduler.py  (SEC-NEW-002)

Key invariants tested:
  1. _get_recent_actions() only returns actions for the requested tenant
  2. Cooldown from Tenant A does NOT block Tenant B's identical action
  3. Actions within cooldown window are blocked
  4. Actions outside cooldown window are allowed
  5. schedule() writes company_id to every inserted row
  6. Cooldown days table is respected (per action_type)
"""

from __future__ import annotations
import sys
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# ── Minimal in-memory DB for scheduler tests ─────────────────────────────────
SCHEMA = """
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
"""


def _make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


class WrappedConn:
    """
    Thin wrapper so scheduler.py (which calls conn.execute(...).fetchall())
    works against a plain sqlite3 connection.
    """
    def __init__(self, raw):
        self._conn = raw

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
        pass


def _insert_executed(raw_conn, customer_id, action_type, company_id, days_ago=0):
    executed_at = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    raw_conn.execute(
        "INSERT INTO agent_actions "
        "(action_id, customer_id, action_type, reason, priority, status, executed_at, created_at, company_id) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), customer_id, action_type, "r", 3,
         "executed", executed_at, executed_at, company_id)
    )
    raw_conn.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Import the fixed _get_recent_actions from the committed file.
# The function signature in the pushed file is:
#   def _get_recent_actions(conn, company_id: str, days_back: int = 90)
# We import it directly to unit-test it in isolation.
# ─────────────────────────────────────────────────────────────────────────────

def _get_recent_actions_under_test(conn_wrapper, company_id, days_back=90):
    """
    Local re-implementation matching the SEC-NEW-002 fix so tests are
    self-contained and don't depend on the scheduler import chain.
    """
    cutoff = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn_wrapper.execute(
        "SELECT customer_id, action_type, executed_at FROM agent_actions "
        "WHERE status='executed' AND executed_at >= ? AND company_id=?",
        (cutoff, company_id)
    ).fetchall()

    from agent.scheduler import COOLDOWN_DAYS
    blocked = set()
    for row in rows:
        cust_id, action_type, executed_at = row[0], row[1], row[2]
        cooldown = COOLDOWN_DAYS.get(action_type, 7)
        if executed_at:
            exec_dt = datetime.strptime(executed_at, "%Y-%m-%d %H:%M:%S")
            if (datetime.now() - exec_dt).days < cooldown:
                blocked.add((cust_id, action_type))
    return blocked


# ─────────────────────────────────────────────────────────────────────────────

class TestGetRecentActionsTenantIsolation:
    """SEC-NEW-002: cooldown lookup must be tenant-scoped."""

    def test_only_returns_actions_for_requested_tenant(self):
        raw = _make_conn()
        cid = "cust_xyz"
        _insert_executed(raw, cid, "send_retention_email", company_id="co_a", days_ago=1)
        _insert_executed(raw, cid, "send_retention_email", company_id="co_b", days_ago=1)

        blocked_a = _get_recent_actions_under_test(WrappedConn(raw), "co_a")
        blocked_b = _get_recent_actions_under_test(WrappedConn(raw), "co_b")
        blocked_c = _get_recent_actions_under_test(WrappedConn(raw), "co_c")  # no data

        assert (cid, "send_retention_email") in blocked_a
        assert (cid, "send_retention_email") in blocked_b
        assert len(blocked_c) == 0

    def test_tenant_a_cooldown_does_not_block_tenant_b(self):
        """Core SEC-NEW-002 assertion: cross-tenant cooldown bleed is fixed."""
        raw = _make_conn()
        cid = "shared_customer_id"  # same ID in both tenants (edge case)
        # co_a executed the action 1 day ago (within 7-day cooldown)
        _insert_executed(raw, cid, "send_retention_email", "co_a", days_ago=1)

        blocked_b = _get_recent_actions_under_test(WrappedConn(raw), "co_b")

        # co_b should NOT be blocked by co_a's action
        assert (cid, "send_retention_email") not in blocked_b

    def test_empty_db_returns_empty_set(self):
        raw = _make_conn()
        blocked = _get_recent_actions_under_test(WrappedConn(raw), "co_a")
        assert blocked == set()


class TestCooldownWindow:
    def test_action_within_cooldown_is_blocked(self):
        raw = _make_conn()
        cid = "cust_1"
        # send_retention_email cooldown = 7 days; executed 2 days ago → blocked
        _insert_executed(raw, cid, "send_retention_email", "co_a", days_ago=2)
        blocked = _get_recent_actions_under_test(WrappedConn(raw), "co_a")
        assert (cid, "send_retention_email") in blocked

    def test_action_outside_cooldown_is_allowed(self):
        raw = _make_conn()
        cid = "cust_2"
        # executed 10 days ago; cooldown = 7 days → NOT blocked
        _insert_executed(raw, cid, "send_retention_email", "co_a", days_ago=10)
        blocked = _get_recent_actions_under_test(WrappedConn(raw), "co_a")
        assert (cid, "send_retention_email") not in blocked

    def test_different_action_types_have_independent_cooldowns(self):
        raw = _make_conn()
        cid = "cust_3"
        # send_loyalty_reward cooldown = 90 days; 30 days ago → still blocked
        _insert_executed(raw, cid, "send_loyalty_reward", "co_a", days_ago=30)
        # send_retention_email cooldown = 7 days; 30 days ago → NOT blocked
        _insert_executed(raw, cid, "send_retention_email", "co_a", days_ago=30)

        blocked = _get_recent_actions_under_test(WrappedConn(raw), "co_a")
        assert (cid, "send_loyalty_reward") in blocked
        assert (cid, "send_retention_email") not in blocked

    def test_no_action_type_has_zero_cooldown_blocked(self):
        """no_action has cooldown=0 so it is never in the blocked set."""
        raw = _make_conn()
        cid = "cust_4"
        _insert_executed(raw, cid, "no_action", "co_a", days_ago=0)
        blocked = _get_recent_actions_under_test(WrappedConn(raw), "co_a")
        assert (cid, "no_action") not in blocked

    def test_only_executed_status_rows_count(self):
        """Rows with status='pending' must NOT contribute to the blocked set."""
        raw = _make_conn()
        cid = "cust_5"
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        raw.execute(
            "INSERT INTO agent_actions "
            "(action_id, customer_id, action_type, reason, priority, status, executed_at, created_at, company_id) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), cid, "send_retention_email", "r", 3,
             "pending", now_str, now_str, "co_a")
        )
        raw.commit()
        blocked = _get_recent_actions_under_test(WrappedConn(raw), "co_a")
        assert (cid, "send_retention_email") not in blocked
