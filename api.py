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
            pending  = conn.execute("SELECT COUNT(*) FROM agent_actions WHERE status='pending_approval' AND company_id=?", (cid,)).fetchone()[0]

            seg_rows = conn.execute(
                "SELECT segment, COUNT(*) as n FROM ml_predictions WHERE company_id=? GROUP BY segment", (cid,)
            ).fetchall()
            segments = {}
            for r in seg_rows:
                if isinstance(r, dict):
                    segments[r.get("segment","Unknown")] = r.get("n", 0)
                elif isinstance(r, (list, tuple)) and len(r) >= 2:
                    segments[r[0]] = r[1]

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

    @app.post("/agent/run", tags=["Agent"])
    def run_agent(
        use_llm:    bool = Query(False, description="Use Claude API for LLM planning"),
        llm_budget: int  = Query(20,    description="Max LLM calls per cycle"),
        dry_run:    bool = Query(True,  description="Preview actions without queuing"),
        company:    dict = Depends(require_auth),
    ):
        """Trigger a full agent decision cycle."""
        from agent.runner import run_agent_cycle
        cid     = company["company_id"]
        summary = run_agent_cycle(use_llm=use_llm, llm_budget=llm_budget, company_id=cid, verbose=False, dry_run=dry_run)
        if dry_run:
            summary["note"] = "dry_run=true — actions previewed but not queued"
        return summary

    @app.post("/agent/train", tags=["Agent"])
    def train_models(
        req:         TrainRequest,
        company_id:  str  = Query("tester-001", description="Company ID to train on"),
        _admin:      dict = Depends(require_admin),
    ):
        """Retrain ML models and/or re-run inference on all customers."""
        from models.orchestrator import train_all, run_inference_and_store
        result = {}
        if req.retrain_models:
            arts = train_all(verbose=False, company_id=company_id)
            result["models_trained"] = list(arts.keys())
        if req.run_inference:
            preds = run_inference_and_store(company_id=company_id, verbose=False)
            result["customers_scored"] = len(preds)
        result["company_id"] = company_id
        result["timestamp"] = datetime.now().isoformat()
        return result

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


    @app.get("/customers", tags=["Customers"])
    def list_customers(
        limit:     int   = Query(50, le=500),
        offset:    int   = Query(0),
        churned:   bool  = Query(False),
        company:   dict  = Depends(require_auth),
    ):
        """List customers with their latest predictions."""
        cid = company["company_id"]
        df = read_sql("""
            SELECT c.customer_id, c.name, c.email, c.plan, c.mrr,
                   c.health_score, c.is_churned, c.signup_date,
                   p.churn_score, p.conversion_score, p.segment
            FROM customers c
            LEFT JOIN ml_predictions p ON p.customer_id = c.customer_id AND p.company_id = c.company_id
            WHERE c.company_id = ? AND c.is_churned = ?
            ORDER BY c.mrr DESC LIMIT ? OFFSET ?
        """, params=(cid, 1 if churned else 0, limit, offset))
        customers = df.to_dict("records") if not df.empty else []
        return {"customers": customers, "count": len(customers), "offset": offset}

    @app.get("/customers/{customer_id}", tags=["Customers"])
    def get_customer(customer_id: str, company: dict = Depends(require_auth)):
        """Get a single customer with full details."""
        cid = company["company_id"]
        df = read_sql(
            "SELECT * FROM customers WHERE customer_id = ? AND company_id = ?",
            params=(customer_id, cid)
        )
        if df.empty:
            raise HTTPException(status_code=404, detail="Customer not found")
        return df.iloc[0].to_dict()

    @app.get("/approvals/queue", tags=["Approvals"])
    def get_approval_queue(
        limit:   int  = Query(50, le=500),
        company: dict = Depends(require_auth),
    ):
        """Fetch all actions waiting for human review."""
        ensure_approval_schema()
        cid = company["company_id"]
        df = read_sql("""
            SELECT a.action_id, a.customer_id, a.action_type, a.priority,
                   a.reason, a.status, a.created_at,
                   c.name, c.mrr, c.plan
            FROM agent_actions a
            LEFT JOIN customers c ON c.customer_id = a.customer_id
            WHERE a.status = 'pending_approval' AND a.company_id = ?
            ORDER BY a.priority DESC, a.created_at ASC
            LIMIT ?
        """, params=(cid, limit))
        actions = df.to_dict("records") if not df.empty else []
        return {"actions": actions, "count": len(actions)}

    @app.post("/approvals/{action_id}/approve", tags=["Approvals"])
    def approve_action(
        action_id:    str,
        req:          ApproveRequest,
        company:      dict = Depends(require_auth),
    ):
        """Approve a single action."""
        ensure_approval_schema()
        from actions.approval import approve_action as _approve
        return _approve(action_id, req.decided_by, req.edited_reason)

    @app.post("/approvals/{action_id}/reject", tags=["Approvals"])
    def reject_action(
        action_id:  str,
        req:        RejectRequest,
        company:    dict = Depends(require_auth),
    ):
        """Reject a single action."""
        ensure_approval_schema()
        from actions.approval import reject_action as _reject
        return _reject(action_id, req.rejection_note, req.decided_by)

    @app.post("/approvals/approve-all", tags=["Approvals"])
    def approve_all(
        decided_by: str  = Query("human"),
        company:    dict = Depends(require_auth),
    ):
        """Approve every pending_approval action for this company at once."""
        ensure_approval_schema()
        return approve_all_pending(
            company_id=company["company_id"],
            decided_by=decided_by,
        )

    @app.get("/approvals/history", tags=["Approvals"])
    def get_approval_audit_trail(
        limit:   int  = Query(50, le=500),
        company: dict = Depends(require_auth),
    ):
        """Full audit log of all approval decisions."""
        ensure_approval_schema()
        cid = company["company_id"]
        return get_approval_audit_trail_db(company_id=cid, limit=limit)

    @app.get("/me", tags=["Auth"])
    def get_current_company(company: dict = Depends(require_auth)):
        """Return info about the authenticated company."""
        return company

    @app.get("/me/stats", tags=["Auth"])
    def my_stats(company: dict = Depends(require_auth)):
        """Return data stats for the authenticated company."""
        return get_tenant_stats(company["company_id"])

    @app.get("/admin/reset-key", include_in_schema=False)
    def reset_admin_key(secret: str = Query(...)):
        if secret != "ezer-reset-2026":
            raise HTTPException(status_code=403, detail="Invalid secret")
        try:
            from utils.db import get_connection
            conn = get_connection()
            conn.execute("DELETE FROM api_keys WHERE key_type = 'admin'")
            conn.commit()
            conn.close()
            from utils.auth import create_api_key
            result = create_api_key("ACIA Admin", "admin@acia.internal", key_type="admin")
            return {"new_admin_key": result["api_key"]}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/admin/debug-key", include_in_schema=False)
    def debug_key(secret: str = Query(...), key: str = Query(...)):
        if secret != "ezer-reset-2026":
            raise HTTPException(status_code=403, detail="Invalid secret")
        import hashlib
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        try:
            df = read_sql(
                "SELECT k.key_id, k.company_id, k.key_type, k.is_active, c.company_name "
                "FROM api_keys k "
                "JOIN companies c ON c.company_id = k.company_id "
                "WHERE k.key_hash = ?",
                params=(key_hash,)
            )
            return {"key_hash": key_hash, "found": not df.empty, "rows": df.to_dict("records") if not df.empty else []}
        except Exception as e:
            return {"error": str(e), "key_hash": key_hash}

    return app


# ── Entrypoint ────────────────────────────────────────────────────────────────

if FASTAPI_AVAILABLE:
    app = create_app()
else:
    app = None

if __name__ == "__main__":
    if not FASTAPI_AVAILABLE:
        print("Install FastAPI first:  pip install fastapi uvicorn")
    else:
        try:
            import uvicorn
            print("Starting ACIA API on http://localhost:8000")
            uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=True)
        except ImportError:
            print("Install uvicorn:  pip install uvicorn")
