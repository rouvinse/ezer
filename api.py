"""
ACIA — REST API (FastAPI)
Exposes the full CRM intelligence system as a REST service.

Install:  pip install fastapi uvicorn
Run:      uvicorn api.main:app --reload --port 8000
Docs:     http://localhost:8000/docs
"""

from __future__ import annotations
import os
from pathlib import Path
from datetime import datetime
from typing import Optional

# FastAPI imports — install with: pip install fastapi uvicorn
try:
    from fastapi import FastAPI, HTTPException, Query, BackgroundTasks, Security, Depends
    from fastapi.security import APIKeyHeader
    from fastapi.middleware.cors import CORSMiddleware
    from pydantic import BaseModel
    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False
    print("FastAPI not installed. Run: pip install fastapi uvicorn")

import sys
sys.path.insert(0, str(Path(__file__).parent))
from utils.db import get_connection, read_sql
from utils.tenancy import tenant_context, get_tenant_stats, list_tenants, provision_tenant
from actions.approval import (
    get_approval_queue, get_approval_stats, approve_action, reject_action,
    bulk_approve, approve_all_pending, get_approval_history,
    set_trust_mode, get_trust_mode, ensure_approval_schema,
    TRUST_MODE_FULL, TRUST_MODE_HIGH_RISK, TRUST_MODE_AUTO
)
from utils.auth import (
    validate_api_key, create_api_key, revoke_api_key,
    list_api_keys, bootstrap_admin_key, ensure_auth_schema
)


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class CustomerRisk(BaseModel):
    customer_id:       str
    name:              str
    plan:              str
    mrr:               float
    churn_score:       float
    churn_risk:        str          # Low / Medium / High
    conversion_score:  float
    conversion_tier:   str          # Cold / Warm / Hot
    segment:           str
    ltv_estimate:      float
    health_score:      float
    predicted_at:      str


class ActionItem(BaseModel):
    action_id:   str
    customer_id: str
    action_type: str
    reason:      str
    priority:    int
    status:      str
    outcome:     Optional[str]
    created_at:  str
    executed_at: Optional[str]


class AgentCycleResult(BaseModel):
    cycle_at:        str
    customers:       int
    candidates:      int
    plans:           int
    queued_actions:  int
    llm_enhanced:    int
    elapsed_sec:     float
    critical:        int
    high:            int


class DashboardSummary(BaseModel):
    total_customers:    int
    active_customers:   int
    churned_customers:  int
    churn_rate:         float
    total_mrr:          float
    total_ltv:          float
    high_risk_count:    int
    hot_conversions:    int
    actions_executed:   int
    actions_pending:    int
    segments:           dict
    generated_at:       str


class ExecuteRequest(BaseModel):
    limit:   int = 50
    dry_run: bool = False

    class Config:
        str_max_length = 500

class ValidatedStr(str):
    """String that strips dangerous characters."""
    @classmethod
    def __get_validators__(cls):
        yield cls.validate

    @classmethod
    def validate(cls, v):
        if not isinstance(v, str):
            raise ValueError("Must be a string")
        # Strip null bytes and control characters
        v = v.replace("\x00", "").strip()
        if len(v) > 1000:
            raise ValueError("String too long")
        return cls(v)

class ApproveRequest(BaseModel):
    decided_by:    str = "human"
    edited_reason: str | None = None

    class Config:
        str_max_length = 2000

class RejectRequest(BaseModel):
    rejection_note: str = ""
    decided_by:     str = "human"

    class Config:
        str_max_length = 2000

class BulkApproveRequest(BaseModel):
    action_ids: list[str]
    decided_by: str = "human"

class TrustModeRequest(BaseModel):
    trust_mode: str   # full_review | high_risk_only | autonomous
    updated_by: str = "admin"


class TrainRequest(BaseModel):
    retrain_models: bool = True
    run_inference:  bool = True


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_db():
    return get_connection()

def row_to_dict(row) -> dict:
    """Convert a DB row to dict. Handles RealDictRow, tuple, list, and dict."""
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    try:
        return dict(row)
    except Exception:
        return {}

def rows_to_dicts(rows, col_names: list) -> list:
    """Convert list of rows (tuples/lists) to list of dicts using column names."""
    result = []
    for row in rows:
        if isinstance(row, dict):
            result.append(dict(row))
        elif isinstance(row, (list, tuple)):
            result.append(dict(zip(col_names, row)))
        else:
            result.append({})
    return result


# ── App factory ───────────────────────────────────────────────────────────────

def create_app() -> "FastAPI":
    app = FastAPI(
        title="ACIA — Autonomous CRM Intelligence Agent",
        description=(
            "AI-powered CRM system that predicts churn and conversion, "
            "then autonomously executes engagement actions.\n\n"
            "Built with: Python · SQLite · scikit-learn · Claude API"
        ),
        version="1.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )


    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Authentication setup ──────────────────────────────────────────────────
    # Ensure auth tables exist and admin key is bootstrapped
    ensure_auth_schema()
    bootstrap_admin_key()

    API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)

    def require_auth(api_key: str = Security(API_KEY_HEADER)) -> dict:
        """
        FastAPI dependency — validates X-API-Key header on every request.
        Inject into any endpoint with: company=Depends(require_auth)
        Returns company info dict on success, raises 401 on failure.
        """
        if not api_key:
            raise HTTPException(
                status_code=401,
                detail="Missing API key. Include X-API-Key header.",
                headers={"WWW-Authenticate": "ApiKey"},
            )
        company = validate_api_key(api_key)
        if not company:
            raise HTTPException(
                status_code=401,
                detail="Invalid or revoked API key.",
                headers={"WWW-Authenticate": "ApiKey"},
            )
        return company

    def require_admin(api_key: str = Security(API_KEY_HEADER)) -> dict:
        """Admin-only dependency — requires an admin key."""
        company = require_auth(api_key)
        if company.get("key_type") != "admin":
            raise HTTPException(
                status_code=403,
                detail="This endpoint requires an admin API key.",
            )
        return company

    # ── Key management endpoints ──────────────────────────────────────────────

    @app.post("/auth/keys", tags=["Auth"])
    def issue_api_key(
        company_name: str,
        email:        str,
        key_type:     str = "live",
        notes:        str = "",
        company_id:   str | None = None,
        _admin: dict  = Depends(require_admin),
    ):
        """
        Issue a new API key for a company. Admin key required.
        Pass company_id to link to an existing provisioned tenant.
        The raw key is returned ONCE — store it securely.
        """
        result = create_api_key(company_name, email, key_type, notes, company_id=company_id)
        return {
            "api_key":      result["api_key"],
            "key_id":       result["key_id"],
            "company_id":   result["company_id"],
            "company_name": result["company_name"],
            "key_type":     result["key_type"],
            "warning":      "Save this key — it will not be shown again.",
        }

    @app.delete("/auth/keys/{key_id}", tags=["Auth"])
    def revoke_key(key_id: str, _admin: dict = Depends(require_admin)):
        """Revoke an API key by key_id. Admin key required."""
        success = revoke_api_key(key_id)
        if not success:
            raise HTTPException(status_code=404, detail="Key not found")
        return {"revoked": True, "key_id": key_id}

    @app.get("/auth/keys", tags=["Auth"])
    def get_all_keys(_admin: dict = Depends(require_admin)):
        """List all API keys. Admin key required."""
        return {"keys": list_api_keys()}

    @app.get("/auth/me", tags=["Auth"])
    def get_current_company(company: dict = Depends(require_auth)):
        """Return the company info for the current API key."""
        return company

    # ── Health ────────────────────────────────────────────────────────────────

    @app.get("/", tags=["Health"])
    def root():
        """Health check — confirms API is running."""
        return {
            "status": "ok",
            "service": "ACIA",
            "version": "1.0.0",
            "timestamp": datetime.now().isoformat(),
        }

    @app.get("/health", tags=["Health"])
    def health():
        """Deep health check — verifies DB connectivity."""
        try:
            conn = get_db()
            n = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
            conn.close()
            return {"status": "ok", "db": "connected", "customers": n}
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"DB error: {e}")

    # ── Dashboard ─────────────────────────────────────────────────────────────

    @app.get("/dashboard", response_model=DashboardSummary, tags=["Dashboard"])
    def get_dashboard(company: dict = Depends(require_auth)):
        """Full dashboard summary — KPIs, segments, action counts."""
        conn = get_db()
        try:
            cid      = company["company_id"]
            total    = conn.execute("SELECT COUNT(*) FROM customers WHERE company_id=?", (cid,)).fetchone()[0]
            active   = conn.execute("SELECT COUNT(*) FROM customers WHERE is_churned=0 AND company_id=?", (cid,)).fetchone()[0]
            churned  = total - active
            mrr      = conn.execute("SELECT COALESCE(SUM(mrr),0) FROM customers WHERE is_churned=0 AND company_id=?", (cid,)).fetchone()[0]
            ltv      = 0  # ltv_estimate not yet implemented
            hi_risk  = conn.execute("SELECT COUNT(*) FROM ml_predictions WHERE churn_score>=0.6 AND company_id=?", (cid,)).fetchone()[0]
            hot_conv = conn.execute("SELECT COUNT(*) FROM ml_predictions WHERE conversion_score>=0.7 AND company_id=?", (cid,)).fetchone()[0]
            executed = conn.execute("SELECT COUNT(*) FROM agent_actions WHERE status='executed' AND company_id=?", (cid,)).fetchone()[0]
            pending  = conn.execute("SELECT COUNT(*) FROM agent_actions WHERE status='pending' AND company_id=?", (cid,)).fetchone()[0]

            seg_rows = conn.execute(
                "SELECT segment, COUNT(*) as n FROM ml_predictions WHERE company_id=? GROUP BY segment", (cid,)
            ).fetchall()
            segments = {r["segment"]: r["n"] for r in seg_rows}

            return DashboardSummary(
                total_customers=total,
                active_customers=active,
                churned_customers=churned,
                churn_rate=round(churned / total * 100, 2) if total else 0,
                total_mrr=round(mrr, 2),
                total_ltv=round(ltv, 2),
                high_risk_count=hi_risk,
                hot_conversions=hot_conv,
                actions_executed=executed,
                actions_pending=pending,
                segments=segments,
                generated_at=datetime.now().isoformat(),
            )
        finally:
            conn.close()

    # ── Customers ─────────────────────────────────────────────────────────────

    @app.get("/customers", tags=["Customers"])
    def list_customers(
        plan:      Optional[str] = Query(None, description="Filter by plan: Free/Starter/Pro/Enterprise"),
        segment:   Optional[str] = Query(None, description="Filter by segment: Champion/Loyal/At-Risk/Prospect/Lost"),
        min_churn: float         = Query(0.0,  description="Minimum churn score (0–1)"),
        max_churn: float         = Query(1.0,  description="Maximum churn score (0–1)"),
        limit:     int           = Query(50,   le=500),
        offset:    int           = Query(0),
        company:   dict          = Depends(require_auth),
    ):
        """List customers with ML predictions. Supports filtering and pagination."""
        conn = get_db()
        try:
            cid    = company["company_id"]
            where  = ["c.is_churned = 0", "p.churn_score >= ?", "p.churn_score <= ?", "c.company_id = ?"]
            params: list = [min_churn, max_churn, cid]

            if plan:
                where.append("c.plan = ?")
                params.append(plan)
            if segment:
                where.append("p.segment = ?")
                params.append(segment)

            sql = f"""
                SELECT c.customer_id, c.name, c.plan, c.mrr, c.health_score,
                       p.churn_score, p.conversion_score, p.segment, p.ltv_estimate
                FROM customers c
                JOIN ml_predictions p ON p.customer_id = c.customer_id
                WHERE {' AND '.join(where)}
                ORDER BY p.churn_score DESC
                LIMIT ? OFFSET ?
            """
            rows = conn.execute(sql, params + [limit, offset]).fetchall()
            return {"customers": [row_to_dict(r) for r in rows], "count": len(rows), "offset": offset}
        finally:
            conn.close()

    @app.get("/customers/{customer_id}/risk", response_model=CustomerRisk, tags=["Customers"])
    def get_customer_risk(customer_id: str, company: dict = Depends(require_auth)):
        """Full risk profile for a single customer."""
        conn = get_db()
        try:
            cid = company["company_id"]
            row = conn.execute("""
                SELECT c.customer_id, c.name, c.plan, c.mrr, c.health_score,
                       p.churn_score, p.conversion_score, p.segment, p.ltv_estimate, p.predicted_at
                FROM customers c
                JOIN ml_predictions p ON p.customer_id = c.customer_id
                WHERE c.customer_id = ? AND c.company_id = ?
            """, (customer_id, cid)).fetchone()[0]

            if not row:
                raise HTTPException(status_code=404, detail=f"Customer {customer_id} not found")

            d = row_to_dict(row)
            d["churn_risk"]      = "High" if d["churn_score"] >= 0.6 else "Medium" if d["churn_score"] >= 0.3 else "Low"
            d["conversion_tier"] = "Hot"  if d["conversion_score"] >= 0.7 else "Warm" if d["conversion_score"] >= 0.4 else "Cold"
            return CustomerRisk(**d)
        finally:
            conn.close()

    @app.get("/customers/{customer_id}/actions", tags=["Customers"])
    def get_customer_actions(customer_id: str, company: dict = Depends(require_auth)):
        """All actions (past + pending) for a specific customer."""
        conn = get_db()
        try:
            cid  = company["company_id"]
            rows = conn.execute(
                "SELECT * FROM agent_actions WHERE customer_id=? AND company_id=? ORDER BY created_at DESC",
                (customer_id, cid)
            ).fetchall()
            return {"customer_id": customer_id, "actions": [row_to_dict(r) for r in rows]}
        finally:
            conn.close()

    # ── Predictions ───────────────────────────────────────────────────────────

    @app.get("/predictions/churn", tags=["Predictions"])
    def get_churn_predictions(
        min_score: float = Query(0.0, description="Minimum churn score"),
        limit:     int   = Query(50,  le=500),
        company:   dict  = Depends(require_auth),
    ):
        """Customers ranked by churn score — highest risk first."""
        cid = company["company_id"]
        df = read_sql("""
            SELECT c.customer_id, c.name, c.plan, c.mrr,
                   p.churn_score, p.segment
            FROM ml_predictions p
            JOIN customers c ON c.customer_id = p.customer_id
            WHERE p.churn_score >= ? AND c.is_churned = 0 AND p.company_id = ?
            ORDER BY p.churn_score DESC LIMIT ?
        """, params=(min_score, cid, limit))
        predictions = df.to_dict("records") if not df.empty else []
        return {"predictions": predictions, "count": len(predictions)}

    @app.get("/predictions/conversion", tags=["Predictions"])
    def get_conversion_predictions(
        min_score: float = Query(0.5, description="Minimum conversion score"),
        limit:     int   = Query(50,  le=500),
        company:   dict  = Depends(require_auth),
    ):
        """Customers ranked by conversion probability — hottest leads first."""
        cid = company["company_id"]
        df = read_sql("""
            SELECT c.customer_id, c.name, c.plan, c.mrr,
                   p.conversion_score, p.segment
            FROM ml_predictions p
            JOIN customers c ON c.customer_id = p.customer_id
            WHERE p.conversion_score >= ? AND c.is_churned = 0 AND p.company_id = ?
            ORDER BY p.conversion_score DESC LIMIT ?
        """, params=(min_score, cid, limit))
        predictions = df.to_dict("records") if not df.empty else []
        return {"predictions": predictions, "count": len(predictions)}

    @app.get("/predictions/segments", tags=["Predictions"])
    def get_segments(
        company: dict = Depends(require_auth),
    ):
        """Customer segmentation breakdown."""
        cid = company["company_id"]
        df = read_sql("""
            SELECT p.segment, COUNT(*) as count,
                   AVG(c.mrr) as avg_mrr,
                   AVG(p.churn_score) as avg_churn_score
            FROM ml_predictions p
            JOIN customers c ON c.customer_id = p.customer_id
            WHERE p.company_id = ?
            GROUP BY p.segment
            ORDER BY count DESC
        """, params=(cid,))
        segments = df.to_dict("records") if not df.empty else []
        return {"segments": segments}


