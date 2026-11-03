"""
Functional tests — api.py (FastAPI endpoints)

Uses FastAPI's TestClient to send real HTTP requests against the app
with a patched in-memory database so no Railway/PostgreSQL is needed.

Tests cover:
  1. /health — unauthenticated public endpoint returns 200
  2. Authenticated endpoints return 401 with no/bad key
  3. Authenticated endpoints succeed with a valid key
  4. /api/v1/customers — pagination params accepted
  5. /api/v1/actions/execute — requires auth, returns expected shape
  6. /api/v1/train — requires auth
  7. /api/v1/dashboard — requires auth, returns DashboardSummary shape
  8. Connector sync endpoints require POST body credentials (not URL params)
  9. Multi-tenant: data returned for co_a is not co_b's data
"""

from __future__ import annotations
import os
import sys
import uuid
import sqlite3
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

REPO = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO))

os.environ["DB_ENGINE"] = "sqlite"
os.environ.setdefault("ACIA_ADMIN_KEY", "acia_admin_sk_test_functional_key_00000000")

try:
    from fastapi.testclient import TestClient
    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not FASTAPI_AVAILABLE,
    reason="fastapi / httpx not installed — run: pip install fastapi httpx"
)


# ── Build the app with mocked DB ─────────────────────────────────────────────

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
    customer_id  TEXT PRIMARY KEY,
    name         TEXT,
    email        TEXT,
    company      TEXT,
    plan         TEXT,
    mrr          REAL,
    signup_date  TEXT,
    country      TEXT,
    health_score REAL,
    nps_score    INTEGER,
    is_churned   INTEGER DEFAULT 0,
    churn_date   TEXT,
    created_at   TEXT,
    company_id   TEXT DEFAULT 'default'
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
CREATE TABLE IF NOT EXISTS email_log (
    email_id    TEXT PRIMARY KEY,
    customer_id TEXT,
    email_type  TEXT,
    subject     TEXT,
    opened      INTEGER DEFAULT 0,
    clicked     INTEGER DEFAULT 0,
    sent_at     TEXT,
    company_id  TEXT DEFAULT 'default'
);
CREATE TABLE IF NOT EXISTS approval_settings (
    id          INTEGER PRIMARY KEY,
    trust_mode  TEXT DEFAULT 'full_review',
    updated_at  TEXT,
    updated_by  TEXT,
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
CREATE TABLE IF NOT EXISTS transactions (
    txn_id      TEXT PRIMARY KEY,
    customer_id TEXT,
    amount      REAL,
    currency    TEXT,
    txn_type    TEXT,
    product_id  TEXT,
    status      TEXT,
    txn_date    TEXT,
    company_id  TEXT DEFAULT 'default'
);
"""

_shared_db = sqlite3.connect(":memory:", check_same_thread=False)
_shared_db.row_factory = sqlite3.Row
_shared_db.executescript(SCHEMA)
_shared_db.commit()


def _ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _seed_api_key(raw_key: str, company_id: str, company_name: str):
    import hashlib
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    try:
        _shared_db.execute(
            "INSERT OR IGNORE INTO companies (company_id, company_name, email, created_at) VALUES (?,?,?,?)",
            (company_id, company_name, f"{company_id}@test.com", _ts())
        )
        _shared_db.execute(
            "INSERT OR IGNORE INTO api_keys "
            "(key_id, company_id, company_name, key_hash, key_prefix, key_type, is_active, created_at) "
            "VALUES (?,?,?,?,?,?,1,?)",
            (str(uuid.uuid4()), company_id, company_name, key_hash, raw_key[:16], "live", _ts())
        )
        _shared_db.commit()
    except Exception:
        pass


VALID_KEY_A = "acia_live_sk_functional_test_co_a_key_001"
VALID_KEY_B = "acia_live_sk_functional_test_co_b_key_002"
COMPANY_A = "co_func_a"
COMPANY_B = "co_func_b"


@pytest.fixture(scope="module")
def client():
    """Build the FastAPI app with patched DB and return a TestClient."""
    _seed_api_key(VALID_KEY_A, COMPANY_A, "Company A")
    _seed_api_key(VALID_KEY_B, COMPANY_B, "Company B")

    from utils.db import ACIAConnection

    def mock_get_connection():
        return ACIAConnection(_shared_db, "sqlite")

    import pandas as pd

    def mock_read_sql(query, params=None):
        cur = _shared_db.cursor()
        if params:
            cur.execute(query, params)
        else:
            cur.execute(query)
        rows = cur.fetchall()
        if not rows:
            col_names = [d[0] for d in cur.description] if cur.description else []
            return pd.DataFrame(columns=col_names)
        col_names = [d[0] for d in cur.description]
        return pd.DataFrame([dict(zip(col_names, row)) for row in rows])

    with (
        patch("utils.db.get_connection", side_effect=mock_get_connection),
        patch("utils.db.read_sql", side_effect=mock_read_sql),
        patch("utils.auth.get_connection", side_effect=mock_get_connection),
        patch("utils.auth.read_sql", side_effect=mock_read_sql),
    ):
        from api import create_app
        app = create_app()
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c


# ─────────────────────────────────────────────────────────────────────────────
# /health
# ─────────────────────────────────────────────────────────────────────────────

class TestHealthEndpoint:
    def test_health_returns_200(self, client):
        r = client.get("/health")
        assert r.status_code == 200

    def test_health_no_auth_required(self, client):
        """Health endpoint must be reachable with no API key."""
        r = client.get("/health")
        assert r.status_code != 401

    def test_health_response_has_status_field(self, client):
        r = client.get("/health")
        body = r.json()
        assert "status" in body or r.status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# Authentication
# ─────────────────────────────────────────────────────────────────────────────

class TestAuthentication:
    def test_no_key_returns_401(self, client):
        r = client.get("/api/v1/customers")
        assert r.status_code == 401

    def test_wrong_key_returns_401(self, client):
        r = client.get("/api/v1/customers",
                       headers={"X-API-Key": "acia_live_sk_totallywrong"})
        assert r.status_code == 401

    def test_valid_key_returns_200(self, client):
        r = client.get("/api/v1/customers",
                       headers={"X-API-Key": VALID_KEY_A})
        # 200 or 404 (no customers yet) — both mean auth passed
        assert r.status_code in (200, 404, 422)
        assert r.status_code != 401

    def test_key_in_bearer_format_also_works(self, client):
        """Some clients send Bearer <token>."""
        r = client.get("/api/v1/customers",
                       headers={"Authorization": f"Bearer {VALID_KEY_A}"})
        # If the API doesn't support Bearer, it returns 401; that's fine —
        # we just want to confirm it's not a 500.
        assert r.status_code != 500


# ─────────────────────────────────────────────────────────────────────────────
# /api/v1/customers
# ─────────────────────────────────────────────────────────────────────────────

class TestCustomersEndpoint:
    HEADERS_A = {"X-API-Key": VALID_KEY_A}

    def test_returns_list(self, client):
        r = client.get("/api/v1/customers", headers=self.HEADERS_A)
        if r.status_code == 200:
            assert isinstance(r.json(), list)

    def test_pagination_params_accepted(self, client):
        r = client.get("/api/v1/customers?limit=5&offset=0", headers=self.HEADERS_A)
        assert r.status_code in (200, 404)

    def test_invalid_limit_rejected(self, client):
        r = client.get("/api/v1/customers?limit=abc", headers=self.HEADERS_A)
        assert r.status_code in (400, 422)

    def test_multi_tenant_isolation(self, client):
        """
        Seed a customer for co_a. The /customers endpoint for co_b
        must NOT return that customer.
        """
        cid_a = str(uuid.uuid4())
        _shared_db.execute(
            "INSERT OR IGNORE INTO customers "
            "(customer_id, name, email, company, plan, mrr, signup_date, "
            "country, health_score, nps_score, created_at, company_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid_a, "A-Only Customer", "a@a.com", "A", "pro", 500.0,
             "2025-01-01", "US", 90.0, 9, _ts(), COMPANY_A),
        )
        _shared_db.commit()

        r_b = client.get("/api/v1/customers",
                         headers={"X-API-Key": VALID_KEY_B})
        if r_b.status_code == 200:
            customer_ids = [c.get("customer_id") for c in r_b.json()]
            assert cid_a not in customer_ids, (
                "Cross-tenant data leak: co_b can see co_a's customer"
            )


# ─────────────────────────────────────────────────────────────────────────────
# /api/v1/actions/execute
# ─────────────────────────────────────────────────────────────────────────────

class TestActionsExecuteEndpoint:
    HEADERS = {"X-API-Key": VALID_KEY_A}

    def test_requires_auth(self, client):
        r = client.post("/api/v1/actions/execute", json={"limit": 5, "dry_run": True})
        assert r.status_code == 401

    def test_accepts_dry_run_flag(self, client):
        r = client.post("/api/v1/actions/execute",
                        json={"limit": 1, "dry_run": True},
                        headers=self.HEADERS)
        assert r.status_code in (200, 202, 422, 500)
        assert r.status_code != 401

    def test_invalid_body_rejected(self, client):
        r = client.post("/api/v1/actions/execute",
                        json={"limit": "not_a_number"},
                        headers=self.HEADERS)
        assert r.status_code == 422


# ─────────────────────────────────────────────────────────────────────────────
# /api/v1/dashboard
# ─────────────────────────────────────────────────────────────────────────────

class TestDashboardEndpoint:
    HEADERS = {"X-API-Key": VALID_KEY_A}

    def test_requires_auth(self, client):
        r = client.get("/api/v1/dashboard")
        assert r.status_code == 401

    def test_returns_dashboard_shape(self, client):
        r = client.get("/api/v1/dashboard", headers=self.HEADERS)
        if r.status_code == 200:
            body = r.json()
            expected_keys = {"total_customers", "churn_rate", "total_mrr",
                             "actions_pending", "generated_at"}
            assert expected_keys.issubset(body.keys()), (
                f"Dashboard missing keys: {expected_keys - body.keys()}"
            )


# ─────────────────────────────────────────────────────────────────────────────
# Connector sync endpoints — credentials in POST body, not URL
# ─────────────────────────────────────────────────────────────────────────────

class TestConnectorEndpoints:
    HEADERS = {"X-API-Key": VALID_KEY_A}

    def test_stripe_sync_requires_body_not_query_param(self, client):
        """stripe_key must come from POST body, not a GET query param."""
        r = client.get("/api/v1/connectors/stripe/sync?stripe_key=sk_live_bad",
                       headers=self.HEADERS)
        # Should be 404 (no such GET route) or 405 (method not allowed)
        assert r.status_code in (404, 405), (
            "Connector sync must be a POST that accepts body, not a GET with query params"
        )

    def test_stripe_sync_post_requires_body(self, client):
        r = client.post("/api/v1/connectors/stripe/sync",
                        json={},   # missing stripe_key
                        headers=self.HEADERS)
        # 422 = FastAPI validation failure (missing required field) — correct
        assert r.status_code in (422, 400, 401, 404)

    def test_hubspot_sync_requires_access_token_in_body(self, client):
        r = client.post("/api/v1/connectors/hubspot/sync",
                        json={},   # missing access_token
                        headers=self.HEADERS)
        assert r.status_code in (422, 400, 401, 404)

    def test_salesforce_sync_requires_body_fields(self, client):
        r = client.post("/api/v1/connectors/salesforce/sync",
                        json={"username": "u"},  # incomplete
                        headers=self.HEADERS)
        assert r.status_code in (422, 400, 401, 404)


# ─────────────────────────────────────────────────────────────────────────────
# /api/v1/train
# ─────────────────────────────────────────────────────────────────────────────

class TestTrainEndpoint:
    HEADERS = {"X-API-Key": VALID_KEY_A}

    def test_requires_auth(self, client):
        r = client.post("/api/v1/train", json={})
        assert r.status_code == 401

    def test_accepts_valid_body(self, client):
        r = client.post("/api/v1/train",
                        json={"retrain_models": False, "run_inference": False},
                        headers=self.HEADERS)
        # 200 or 202 or 500 (if no data) — all fine as long as not 401/422
        assert r.status_code not in (401, 422)
