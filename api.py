"""
ACIA — REST API (FastAPI)
Exposes the full CRM intelligence system as a REST service.

Install:  pip install fastapi uvicorn slowapi
Run:      uvicorn api.main:app --reload --port 8000
Docs:     http://localhost:8000/docs  (dev only)
"""

from __future__ import annotations
import os
import uuid
import time
import logging
import logging.config
from pathlib import Path
from datetime import datetime
from typing import Optional

# ── Structured JSON logging setup ────────────────────────────────────────────
def _configure_logging():
    """Configure JSON structured logging for production, plain text for dev."""
    is_prod = os.environ.get("RAILWAY_ENVIRONMENT") == "production"
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()

    if is_prod:
        try:
            from pythonjsonlogger import jsonlogger  # type: ignore
            handler = logging.StreamHandler()
            formatter = jsonlogger.JsonFormatter(
                fmt="%(asctime)s %(name)s %(levelname)s %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
            handler.setFormatter(formatter)
            root = logging.getLogger()
            root.handlers.clear()
            root.addHandler(handler)
            root.setLevel(log_level)
        except ImportError:
            logging.basicConfig(level=log_level)
    else:
        logging.basicConfig(
            level=log_level,
            format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
            datefmt="%H:%M:%S",
        )

_configure_logging()
logger = logging.getLogger("acia.api")

# FastAPI imports — install with: pip install fastapi uvicorn
try:
    from fastapi import FastAPI, HTTPException, Query, BackgroundTasks, Security, Depends, Request, Response
    from fastapi.security import APIKeyHeader
    from fastapi.middleware.cors import CORSMiddleware
    from starlette.middleware.base import BaseHTTPMiddleware
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
    list_api_keys, bootstrap_admin_key, ensure_auth_schema,
    save_connector_credentials, get_connector_credentials,
    list_connected_connectors, ensure_connector_cred_schema,
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
    model_config = {"str_max_length": 500}
    limit:   int = 50
    dry_run: bool = False

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
    model_config = {"str_max_length": 2000}
    decided_by:    str = "human"
    edited_reason: str | None = None

class RejectRequest(BaseModel):
    model_config = {"str_max_length": 2000}
    rejection_note: str = ""
    decided_by:     str = "human"

class BulkApproveRequest(BaseModel):
    action_ids: list[str]
    decided_by: str = "human"

class TrustModeRequest(BaseModel):
    trust_mode: str   # full_review | high_risk_only | autonomous
    updated_by: str = "admin"


class TrainRequest(BaseModel):
    retrain_models: bool = True
    run_inference:  bool = True


# ── Connector credential schemas (POST body — never URL params) ───────────────

class StripeCredentials(BaseModel):
    stripe_key: str

class HubSpotCredentials(BaseModel):
    access_token: str

class RazorpayCredentials(BaseModel):
    key_id:     str
    key_secret: str

class IntercomCredentials(BaseModel):
    access_token: str

class SalesforceCredentials(BaseModel):
    username:       str
    password:       str
    security_token: str
    client_id:      str
    client_secret:  str
    domain:         str = "login"


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


# ── Request ID + access log middleware ────────────────────────────────────────

_access_logger = logging.getLogger("acia.access")

class RequestContextMiddleware(BaseHTTPMiddleware):
    """
    Attach a unique X-Request-ID to every request/response and emit a
    structured access log line on completion.
    """
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id
        t0 = time.perf_counter()
        response: Response = await call_next(request)
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        response.headers["X-Request-ID"] = request_id
        _access_logger.info(
            "request completed",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": elapsed_ms,
                "client": request.client.host if request.client else "unknown",
            },
        )
        return response


# ── Security headers middleware ───────────────────────────────────────────────

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to every response."""
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; frame-ancestors 'none'"
        )
        response.headers["Permissions-Policy"] = (
            "geolocation=(), microphone=(), camera=()"
        )
        # HSTS — only if we know we're running on HTTPS (Railway/production)
        if os.environ.get("RAILWAY_ENVIRONMENT") == "production":
            response.headers["Strict-Transport-Security"] = (
                "max-age=63072000; includeSubDomains; preload"
            )
        return response


# ── App factory ───────────────────────────────────────────────────────────────

def create_app() -> "FastAPI":
    # Disable interactive docs in production (RAILWAY_ENVIRONMENT set by Railway)
    is_prod = os.environ.get("RAILWAY_ENVIRONMENT") == "production"
    app = FastAPI(
        title="ACIA — Autonomous CRM Intelligence Agent",
        description=(
            "AI-powered CRM system that predicts churn and conversion, "
            "then autonomously executes engagement actions.\n\n"
            "Built with: Python · PostgreSQL · scikit-learn · Claude API"
        ),
        version="1.0.0",
        docs_url=None if is_prod else "/docs",
        redoc_url=None if is_prod else "/redoc",
        openapi_url=None if is_prod else "/openapi.json",
    )

    TRUSTED_ORIGINS = [
        "https://ezerapp.netlify.app",
        "https://magenta-melomakarona-cb0b8e.netlify.app",
        "https://ezercrmai.com",
        "https://www.ezercrmai.com",
        "https://ezeradmin.netlify.app",
    ]

    # Middleware order: outermost first (request context → security headers → CORS → rate limit)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=TRUSTED_ORIGINS,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["X-API-Key", "Content-Type", "Authorization"],
        allow_credentials=False,
    )

    # ── Rate limiting ─────────────────────────────────────────────────────────
    try:
        from slowapi import Limiter, _rate_limit_exceeded_handler
        from slowapi.util import get_remote_address
        from slowapi.errors import RateLimitExceeded

        limiter = Limiter(key_func=get_remote_address, default_limits=["200/minute"])
        app.state.limiter = limiter
        app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
        RATE_LIMITING = True
    except ImportError:
        RATE_LIMITING = False
        limiter = None

    # ── Lifespan (replaces deprecated on_event) ───────────────────────────────
    from contextlib import asynccontextmanager  # noqa: F401

    @asynccontextmanager
    async def _lifespan(app_instance):  # type: ignore[override]
        logger.info(
            "ACIA API starting",
            extra={
                "version": "1.0.0",
                "env": os.environ.get("RAILWAY_ENVIRONMENT", "development"),
                "db_engine": os.environ.get("DB_ENGINE", "sqlite"),
                "rate_limiting": RATE_LIMITING,
            },
        )
        # Ensure connector cred table is ready on startup
        try:
            ensure_connector_cred_schema()
        except Exception as _e:
            logger.warning("connector_cred schema setup failed: %s", _e)
        yield
        logger.info("ACIA API shutting down gracefully")

    app.router.lifespan_context = _lifespan

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

    # ── Self-serve registration ───────────────────────────────────────────────

    class RegisterRequest(BaseModel):
        name:         str
        email:        str
        company_name: str
        password:     str  # stored client-side only; not used server-side for auth

    @app.post("/auth/register", tags=["Auth"])
    def self_serve_register(req: RegisterRequest):
        """
        Public self-serve signup.
        Creates a unique company_id + live API key, provisions 100 synthetic
        demo customers so the dashboard shows real data immediately.
        Returns the API key — store it, it is shown only once.
        """
        import re as _re
        if not req.email or not _re.match(r"[^@]+@[^@]+\.[^@]+", req.email):
            raise HTTPException(status_code=422, detail="Invalid email address.")
        if not req.company_name.strip():
            raise HTTPException(status_code=422, detail="Company name is required.")
        if len(req.password) < 8:
            raise HTTPException(status_code=422, detail="Password must be at least 8 characters.")

        # Create API key (auto-generates a unique company_id)
        result = create_api_key(
            company_name=req.company_name.strip(),
            email=req.email.strip().lower(),
            key_type="live",
            notes=f"Self-serve signup by {req.name}",
        )
        company_id = result["company_id"]

        # Provision demo data so dashboard isn't empty
        try:
            from utils.tenancy import provision_tenant
            provision_tenant(
                company_id=company_id,
                company_name=req.company_name.strip(),
                num_customers=100,
            )
        except Exception as e:
            logger.warning("provision_tenant failed for %s: %s", company_id, e)

        # Train models in background (non-blocking best-effort)
        try:
            from models.orchestrator import train_all, run_inference_and_store
            train_all(verbose=False, company_id=company_id)
            run_inference_and_store(verbose=False, company_id=company_id)
        except Exception as e:
            logger.warning("model training failed for %s: %s", company_id, e)

        return {
            "api_key":      result["api_key"],
            "key_id":       result["key_id"],
            "company_id":   company_id,
            "company_name": result["company_name"],
            "warning":      "Save this key — it will not be shown again.",
        }

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
        try:
            keys = list_api_keys()
            # Sanitize — convert any non-serializable values
            clean = []
            for k in keys:
                clean.append({
                    "key_id":       str(k.get("key_id","") or ""),
                    "company_id":   str(k.get("company_id","") or ""),
                    "company_name": str(k.get("company_name","") or ""),
                    "key_prefix":   str(k.get("key_prefix","") or ""),
                    "key_type":     str(k.get("key_type","") or ""),
                    "is_active":    bool(k.get("is_active",0)),
                    "created_at":   str(k.get("created_at","") or ""),
                    "last_used_at": str(k.get("last_used_at","") or "") if k.get("last_used_at") else None,
                    "revoked_at":   str(k.get("revoked_at","") or "") if k.get("revoked_at") else None,
                })
            return clean
        except Exception:
            raise HTTPException(status_code=500, detail="Failed to retrieve keys")

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
        except Exception:
            raise HTTPException(status_code=503, detail="Database unavailable")

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

    # ── Agent ─────────────────────────────────────────────────────────────────

    @app.post("/agent/run", tags=["Agent"])
    def run_agent(
        background_tasks: BackgroundTasks,
        use_llm:    bool = Query(False, description="Use Claude API for LLM planning"),
        llm_budget: int  = Query(20,    description="Max LLM calls per cycle"),
        dry_run:    bool = Query(True,  description="Preview actions without queuing"),
        company:    dict = Depends(require_auth),
    ):
        """
        Trigger a full agent decision cycle.
        dry_run=true  — preview only, returns immediately with counts.
        dry_run=false — queues actions in background, returns immediately.
        """
        from agent.runner import run_agent_cycle
        cid = company["company_id"]

        if dry_run:
            # Dry run is fast enough to run synchronously
            summary = run_agent_cycle(use_llm=use_llm, llm_budget=llm_budget,
                                      company_id=cid, verbose=False, dry_run=True)
            summary["note"] = "dry_run=true — actions previewed but not queued"
            return summary

        # Live run — execute in background to avoid request timeout
        def _run_in_bg():
            try:
                run_agent_cycle(use_llm=use_llm, llm_budget=llm_budget,
                                company_id=cid, verbose=False, dry_run=False)
            except Exception as _e:
                logger.error("background agent cycle failed: %s", _e)

        background_tasks.add_task(_run_in_bg)
        return {
            "status":  "started",
            "message": "Agent cycle running in background. Check /approvals/queue in ~10 seconds.",
            "dry_run": False,
        }

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

    # ── Customers ─────────────────────────────────────────────────────────────

    @app.get("/customers", tags=["Customers"])
    def list_customers(
        limit:     int   = Query(50, ge=1, le=500),
        offset:    int   = Query(0),
        churned:   bool  = Query(False),
        search:    str   = Query("", description="Search by name or email (partial match)"),
        company:   dict  = Depends(require_auth),
    ):
        """List customers with their latest predictions. Supports name/email search."""
        cid = company["company_id"]
        if search:
            pattern = f"%{search}%"
            df = read_sql("""
                SELECT c.customer_id, c.name, c.email, c.plan, c.mrr,
                       c.health_score, c.is_churned, c.signup_date,
                       p.churn_score, p.conversion_score, p.segment
                FROM customers c
                LEFT JOIN ml_predictions p ON p.customer_id = c.customer_id AND p.company_id = c.company_id
                WHERE c.company_id = ? AND c.is_churned = ?
                  AND (LOWER(c.name) LIKE LOWER(?) OR LOWER(c.email) LIKE LOWER(?))
                ORDER BY c.mrr DESC LIMIT ? OFFSET ?
            """, params=(cid, 1 if churned else 0, pattern, pattern, limit, offset))
        else:
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

    # ── Approvals ─────────────────────────────────────────────────────────────

    @app.get("/approvals/queue", tags=["Approvals"])
    def get_approval_queue_endpoint(
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
    def approve_action_endpoint(
        action_id:    str,
        req:          ApproveRequest,
        company:      dict = Depends(require_auth),
    ):
        """Approve a single action."""
        ensure_approval_schema()
        cid = company["company_id"]
        # Check action exists and belongs to this company
        df = read_sql(
            "SELECT action_id FROM agent_actions WHERE action_id = ? AND company_id = ?",
            params=(action_id, cid)
        )
        if df.empty:
            raise HTTPException(status_code=404, detail="Action not found")
        from actions.approval import approve_action as _approve
        result = _approve(action_id, req.decided_by, req.edited_reason, company_id=cid)
        if result.get("success") is False:
            raise HTTPException(status_code=409, detail=result.get("error", "Could not approve action"))
        return result

    @app.post("/approvals/{action_id}/reject", tags=["Approvals"])
    def reject_action_endpoint(
        action_id:  str,
        req:        RejectRequest,
        company:    dict = Depends(require_auth),
    ):
        """Reject a single action."""
        ensure_approval_schema()
        cid = company["company_id"]
        df = read_sql(
            "SELECT action_id FROM agent_actions WHERE action_id = ? AND company_id = ?",
            params=(action_id, cid)
        )
        if df.empty:
            raise HTTPException(status_code=404, detail="Action not found")
        from actions.approval import reject_action as _reject
        result = _reject(action_id, req.rejection_note, req.decided_by, company_id=cid)
        if result.get("success") is False:
            raise HTTPException(status_code=409, detail=result.get("error", "Could not reject action"))
        return result

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
        limit:   int  = Query(50, ge=1, le=500),
        company: dict = Depends(require_auth),
    ):
        """Full audit log of all approval decisions."""
        ensure_approval_schema()
        cid = company["company_id"]
        try:
            # rejection_note lives in approval_log, not agent_actions.
            # Join to get it where available; fall back to NULL for older rows.
            df = read_sql("""
                SELECT a.action_id, a.customer_id, a.action_type, a.priority,
                       a.reason, a.status, a.outcome, a.created_at, a.executed_at,
                       a.decided_by,
                       al.rejection_note
                FROM agent_actions a
                LEFT JOIN approval_log al
                       ON al.action_id = a.action_id
                WHERE a.company_id = ? AND a.status IN ('approved','rejected','executed')
                ORDER BY a.created_at DESC
                LIMIT ?
            """, params=(cid, limit))
            history = df.to_dict("records") if not df.empty else []
            return {"history": history, "count": len(history)}
        except Exception as _he:
            logger.warning("approval history query failed: %s", _he)
            # Fallback: query without the join in case approval_log doesn't exist yet
            try:
                df2 = read_sql("""
                    SELECT action_id, customer_id, action_type, priority,
                           reason, status, outcome, created_at, executed_at,
                           decided_by
                    FROM agent_actions
                    WHERE company_id = ? AND status IN ('approved','rejected','executed')
                    ORDER BY created_at DESC
                    LIMIT ?
                """, params=(cid, limit))
                history = df2.to_dict("records") if not df2.empty else []
                return {"history": history, "count": len(history)}
            except Exception as _he2:
                logger.error("approval history fallback also failed: %s", _he2)
                raise HTTPException(status_code=500, detail="History query failed")

    # ── Trust mode ────────────────────────────────────────────────────────────

    @app.get("/settings/trust-mode", tags=["Settings"])
    def get_trust_mode_endpoint(company: dict = Depends(require_auth)):
        """Get the current trust mode for this company."""
        try:
            mode = get_trust_mode(company["company_id"])
            return {"trust_mode": mode, "company_id": company["company_id"]}
        except Exception:
            raise HTTPException(status_code=500, detail="Failed to retrieve trust mode")

    @app.post("/settings/trust-mode", tags=["Settings"])
    def set_trust_mode_endpoint(
        req:     TrustModeRequest,
        company: dict = Depends(require_auth),
    ):
        """Update the trust mode for this company."""
        valid_modes = {TRUST_MODE_FULL, TRUST_MODE_HIGH_RISK, TRUST_MODE_AUTO}
        if req.trust_mode not in valid_modes:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid trust_mode. Must be one of: {', '.join(sorted(valid_modes))}"
            )
        try:
            set_trust_mode(req.trust_mode, company["company_id"], req.updated_by)
            return {"trust_mode": req.trust_mode, "updated_by": req.updated_by, "updated": True}
        except Exception:
            raise HTTPException(status_code=500, detail="Failed to update trust mode")

    # ── Me / tenant stats ─────────────────────────────────────────────────────

    @app.get("/me/stats", tags=["Auth"])
    def my_stats(company: dict = Depends(require_auth)):
        """Return data stats for the authenticated company."""
        return get_tenant_stats(company["company_id"])

    # ── ALL CONNECTORS ────────────────────────────────────────────────────────

    # Helper to get connector status
    def _connector_status(cid: str) -> dict:
        conn = get_connection()
        result = {
            "customers":    conn.execute("SELECT COUNT(*) FROM customers WHERE company_id=?", (cid,)).fetchone()[0],
            "active":       conn.execute("SELECT COUNT(*) FROM customers WHERE company_id=? AND is_churned=0", (cid,)).fetchone()[0],
            "transactions": conn.execute("SELECT COUNT(*) FROM transactions WHERE company_id=?", (cid,)).fetchone()[0],
            "total_mrr":    conn.execute("SELECT COALESCE(SUM(mrr),0) FROM customers WHERE company_id=? AND is_churned=0", (cid,)).fetchone()[0],
            "events":       conn.execute("SELECT COUNT(*) FROM engagement_events WHERE company_id=?", (cid,)).fetchone()[0],
            "tickets":      conn.execute("SELECT COUNT(*) FROM support_tickets WHERE company_id=?", (cid,)).fetchone()[0],
        }
        conn.close()
        return result

    # ── STRIPE ──────────────────────────────────────────────────────────────
    @app.post("/connectors/stripe/test", tags=["Connectors"])
    def stripe_test_key(
        creds:   StripeCredentials,
        company: dict = Depends(require_auth),
    ):
        """Test if a Stripe secret key is valid."""
        stripe_key = creds.stripe_key
        if not stripe_key.startswith(("sk_live_", "sk_test_")):
            raise HTTPException(status_code=400, detail="Invalid Stripe key. Must start with sk_live_ or sk_test_")
        try:
            import stripe as stripe_lib
            stripe_lib.api_key = stripe_key
            account = stripe_lib.Account.retrieve()
            return {
                "valid": True,
                "account_name": account.get("business_profile", {}).get("name") or account.get("email"),
                "country": account.get("country"),
                "mode": "live" if "live" in stripe_key else "test",
            }
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/connectors/stripe/connect", tags=["Connectors"])
    def stripe_connect(
        creds:   StripeCredentials,
        company: dict = Depends(require_auth),
    ):
        """Connect Stripe — validates key, pulls all customers and transactions, saves credentials for future syncs."""
        stripe_key = creds.stripe_key
        if not stripe_key.startswith(("sk_live_", "sk_test_")):
            raise HTTPException(status_code=400, detail="Invalid Stripe key")
        try:
            import stripe as stripe_lib
            stripe_lib.api_key = stripe_key
            account = stripe_lib.Account.retrieve()
            account_name = account.get("business_profile", {}).get("name") or account.get("email", "Unknown")
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Stripe connection failed: {e}")
        # Save credentials for future syncs (encrypted at rest if ACIA_CRED_KEY is set)
        save_connector_credentials(company["company_id"], "stripe", {"stripe_key": stripe_key})
        try:
            from connectors.stripe_connector import sync_stripe
            summary = sync_stripe(
                company_id=company["company_id"],
                stripe_key=stripe_key,
                fetch_invoices=True,
                verbose=False,
            )
            summary["account_name"] = account_name
            summary["mode"] = "live" if "live" in stripe_key else "test"
            summary["connector"] = "stripe"
            return summary
        except Exception:
            raise HTTPException(status_code=500, detail="Stripe sync failed")

    @app.post("/connectors/stripe/sync", tags=["Connectors"])
    def stripe_sync(company: dict = Depends(require_auth)):
        """Re-sync Stripe data using saved credentials."""
        saved = get_connector_credentials(company["company_id"], "stripe")
        if not saved or not saved.get("stripe_key"):
            raise HTTPException(status_code=400, detail="Stripe not connected. Use POST /connectors/stripe/connect first.")
        try:
            from connectors.stripe_connector import sync_stripe
            summary = sync_stripe(
                company_id=company["company_id"],
                stripe_key=saved["stripe_key"],
                fetch_invoices=True,
                verbose=False,
            )
            summary["connector"] = "stripe"
            return summary
        except Exception:
            raise HTTPException(status_code=500, detail="Stripe re-sync failed")

    @app.get("/connectors/stripe/status", tags=["Connectors"])
    def stripe_status(company: dict = Depends(require_auth)):
        """Current Stripe data in EZER database."""
        return _connector_status(company["company_id"])

    # ── HUBSPOT ─────────────────────────────────────────────────────────────
    @app.post("/connectors/hubspot/test", tags=["Connectors"])
    def hubspot_test(
        creds:   HubSpotCredentials,
        company: dict = Depends(require_auth),
    ):
        """Test HubSpot access token."""
        import requests as req_lib
        try:
            r = req_lib.get(
                "https://api.hubapi.com/crm/v3/objects/contacts?limit=1",
                headers={"Authorization": f"Bearer {creds.access_token}"},
                timeout=10,
            )
            if r.status_code == 401:
                raise HTTPException(status_code=400, detail="Invalid HubSpot token")
            if not r.ok:
                raise HTTPException(status_code=400, detail=f"HubSpot returned {r.status_code}")
            return {"valid": True, "message": "HubSpot connected successfully"}
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=400, detail="Could not reach HubSpot — check your token")

    @app.post("/connectors/hubspot/connect", tags=["Connectors"])
    def hubspot_connect(
        creds:   HubSpotCredentials,
        company: dict = Depends(require_auth),
    ):
        """Connect HubSpot — pulls contacts, companies, deals, and engagement. Saves credentials."""
        save_connector_credentials(company["company_id"], "hubspot", {"access_token": creds.access_token})
        try:
            from connectors.hubspot_connector import sync_hubspot
            summary = sync_hubspot(
                company_id=company["company_id"],
                access_token=creds.access_token,
                verbose=False,
            )
            summary["connector"] = "hubspot"
            return summary
        except Exception:
            raise HTTPException(status_code=500, detail="HubSpot sync failed")

    @app.post("/connectors/hubspot/sync", tags=["Connectors"])
    def hubspot_sync(company: dict = Depends(require_auth)):
        """Re-sync HubSpot data using saved credentials."""
        saved = get_connector_credentials(company["company_id"], "hubspot")
        if not saved or not saved.get("access_token"):
            raise HTTPException(status_code=400, detail="HubSpot not connected. Use POST /connectors/hubspot/connect first.")
        try:
            from connectors.hubspot_connector import sync_hubspot
            summary = sync_hubspot(
                company_id=company["company_id"],
                access_token=saved["access_token"],
                verbose=False,
            )
            summary["connector"] = "hubspot"
            return summary
        except Exception:
            raise HTTPException(status_code=500, detail="HubSpot re-sync failed")

    @app.get("/connectors/hubspot/status", tags=["Connectors"])
    def hubspot_status(company: dict = Depends(require_auth)):
        """Current HubSpot data in EZER database."""
        return _connector_status(company["company_id"])

    # ── RAZORPAY ────────────────────────────────────────────────────────────
    @app.post("/connectors/razorpay/test", tags=["Connectors"])
    def razorpay_test(
        creds:   RazorpayCredentials,
        company: dict = Depends(require_auth),
    ):
        """Test Razorpay credentials."""
        import requests as req_lib
        try:
            r = req_lib.get(
                "https://api.razorpay.com/v1/customers?count=1",
                auth=(creds.key_id, creds.key_secret),
                timeout=10,
            )
            if r.status_code == 401:
                raise HTTPException(status_code=400, detail="Invalid Razorpay credentials")
            if not r.ok:
                raise HTTPException(status_code=400, detail=f"Razorpay returned {r.status_code}")
            return {"valid": True, "message": "Razorpay connected successfully"}
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=400, detail="Could not reach Razorpay — check your credentials")

    @app.post("/connectors/razorpay/connect", tags=["Connectors"])
    def razorpay_connect(
        creds:   RazorpayCredentials,
        company: dict = Depends(require_auth),
    ):
        """Connect Razorpay — pulls customers and payment history. Saves credentials."""
        save_connector_credentials(company["company_id"], "razorpay", {"key_id": creds.key_id, "key_secret": creds.key_secret})
        try:
            from connectors.razorpay_connector import sync_razorpay
            summary = sync_razorpay(
                company_id=company["company_id"],
                key_id=creds.key_id,
                key_secret=creds.key_secret,
                verbose=False,
            )
            summary["connector"] = "razorpay"
            return summary
        except Exception:
            raise HTTPException(status_code=500, detail="Razorpay sync failed")

    @app.post("/connectors/razorpay/sync", tags=["Connectors"])
    def razorpay_sync(company: dict = Depends(require_auth)):
        """Re-sync Razorpay data using saved credentials."""
        saved = get_connector_credentials(company["company_id"], "razorpay")
        if not saved or not saved.get("key_id"):
            raise HTTPException(status_code=400, detail="Razorpay not connected. Use POST /connectors/razorpay/connect first.")
        try:
            from connectors.razorpay_connector import sync_razorpay
            summary = sync_razorpay(
                company_id=company["company_id"],
                key_id=saved["key_id"],
                key_secret=saved["key_secret"],
                verbose=False,
            )
            summary["connector"] = "razorpay"
            return summary
        except Exception:
            raise HTTPException(status_code=500, detail="Razorpay re-sync failed")

    @app.get("/connectors/razorpay/status", tags=["Connectors"])
    def razorpay_status(company: dict = Depends(require_auth)):
        """Current Razorpay data in EZER database."""
        return _connector_status(company["company_id"])

    # ── INTERCOM ─────────────────────────────────────────────────────────────
    @app.post("/connectors/intercom/test", tags=["Connectors"])
    def intercom_test(
        creds:   IntercomCredentials,
        company: dict = Depends(require_auth),
    ):
        """Test Intercom access token."""
        import requests as req_lib
        try:
            r = req_lib.get(
                "https://api.intercom.io/me",
                headers={
                    "Authorization": f"Bearer {creds.access_token}",
                    "Accept": "application/json"
                },
                timeout=10,
            )
            if r.status_code == 401:
                raise HTTPException(status_code=400, detail="Invalid Intercom token")
            if not r.ok:
                raise HTTPException(status_code=400, detail=f"Intercom returned {r.status_code}")
            data = r.json()
            return {
                "valid": True,
                "app_name": data.get("app", {}).get("name", "Unknown"),
            }
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=400, detail="Could not reach Intercom — check your token")

    @app.post("/connectors/intercom/connect", tags=["Connectors"])
    def intercom_connect(
        creds:   IntercomCredentials,
        company: dict = Depends(require_auth),
    ):
        """Connect Intercom — pulls conversations, contacts, and support data. Saves credentials."""
        save_connector_credentials(company["company_id"], "intercom", {"access_token": creds.access_token})
        try:
            from connectors.intercom_connector import sync_intercom
            summary = sync_intercom(
                company_id=company["company_id"],
                access_token=creds.access_token,
                verbose=False,
            )
            summary["connector"] = "intercom"
            return summary
        except Exception:
            raise HTTPException(status_code=500, detail="Intercom sync failed")

    @app.post("/connectors/intercom/sync", tags=["Connectors"])
    def intercom_sync(company: dict = Depends(require_auth)):
        """Re-sync Intercom data using saved credentials."""
        saved = get_connector_credentials(company["company_id"], "intercom")
        if not saved or not saved.get("access_token"):
            raise HTTPException(status_code=400, detail="Intercom not connected. Use POST /connectors/intercom/connect first.")
        try:
            from connectors.intercom_connector import sync_intercom
            summary = sync_intercom(
                company_id=company["company_id"],
                access_token=saved["access_token"],
                verbose=False,
            )
            summary["connector"] = "intercom"
            return summary
        except Exception:
            raise HTTPException(status_code=500, detail="Intercom re-sync failed")

    @app.get("/connectors/intercom/status", tags=["Connectors"])
    def intercom_status(company: dict = Depends(require_auth)):
        """Current Intercom data in EZER database."""
        return _connector_status(company["company_id"])

    # ── SALESFORCE ───────────────────────────────────────────────────────────
    @app.post("/connectors/salesforce/test", tags=["Connectors"])
    def salesforce_test(
        creds:   SalesforceCredentials,
        company: dict = Depends(require_auth),
    ):
        """Test Salesforce credentials."""
        try:
            from connectors.salesforce_connector import test_connection
            ok = test_connection(
                username=creds.username,
                password=creds.password,
                security_token=creds.security_token,
                client_id=creds.client_id,
                client_secret=creds.client_secret,
                domain=creds.domain,
            )
            if not ok:
                raise HTTPException(status_code=400, detail="Salesforce connection failed. Check your credentials.")
            return {"valid": True, "message": "Salesforce connected successfully"}
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=400, detail="Salesforce connection failed — check credentials")

    @app.post("/connectors/salesforce/connect", tags=["Connectors"])
    def salesforce_connect(
        creds:   SalesforceCredentials,
        company: dict = Depends(require_auth),
    ):
        """Connect Salesforce — pulls accounts, contacts, opportunities. Saves credentials."""
        save_connector_credentials(company["company_id"], "salesforce", {
            "username": creds.username,
            "password": creds.password,
            "security_token": creds.security_token,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "domain": creds.domain,
        })
        try:
            from connectors.salesforce_connector import sync_salesforce
            summary = sync_salesforce(
                company_id=company["company_id"],
                username=creds.username,
                password=creds.password,
                security_token=creds.security_token,
                client_id=creds.client_id,
                client_secret=creds.client_secret,
                domain=creds.domain,
                verbose=False,
            )
            summary["connector"] = "salesforce"
            return summary
        except Exception:
            raise HTTPException(status_code=500, detail="Salesforce sync failed")

    @app.post("/connectors/salesforce/sync", tags=["Connectors"])
    def salesforce_sync(company: dict = Depends(require_auth)):
        """Re-sync Salesforce data using saved credentials."""
        saved = get_connector_credentials(company["company_id"], "salesforce")
        if not saved or not saved.get("username"):
            raise HTTPException(status_code=400, detail="Salesforce not connected. Use POST /connectors/salesforce/connect first.")
        try:
            from connectors.salesforce_connector import sync_salesforce
            summary = sync_salesforce(
                company_id=company["company_id"],
                verbose=False,
                **{k: saved[k] for k in ["username","password","security_token","client_id","client_secret","domain"] if k in saved},
            )
            summary["connector"] = "salesforce"
            return summary
        except Exception:
            raise HTTPException(status_code=500, detail="Salesforce re-sync failed")

    @app.get("/connectors/salesforce/status", tags=["Connectors"])
    def salesforce_status(company: dict = Depends(require_auth)):
        """Current Salesforce data in EZER database."""
        return _connector_status(company["company_id"])

    # ── CONNECTOR OVERVIEW ───────────────────────────────────────────────────
    @app.get("/connectors", tags=["Connectors"])
    def list_connectors(company: dict = Depends(require_auth)):
        """List all available connectors, which are connected, and data summary."""
        cid = company["company_id"]
        status = _connector_status(cid)
        connected = list_connected_connectors(cid)
        all_connectors = [
            {"name": "stripe",     "label": "Stripe",     "fields": ["stripe_key"]},
            {"name": "hubspot",    "label": "HubSpot",    "fields": ["access_token"]},
            {"name": "razorpay",   "label": "Razorpay",   "fields": ["key_id","key_secret"]},
            {"name": "intercom",   "label": "Intercom",   "fields": ["access_token"]},
            {"name": "salesforce", "label": "Salesforce", "fields": ["username","password","security_token","client_id","client_secret"]},
        ]
        for c in all_connectors:
            c["status"] = "connected" if c["name"] in connected else "available"
        return {
            "connectors": all_connectors,
            "connected_count": len(connected),
            "data_summary": status,
        }

    # ── Admin: tenant provisioning ────────────────────────────────────────────

    class ProvisionRequest(BaseModel):
        company_id:    str
        company_name:  str
        num_customers: int = 100

    @app.post("/admin/provision", tags=["Admin"])
    def provision_tenant_endpoint(
        req:    ProvisionRequest,
        _admin: dict = Depends(require_admin),
    ):
        """
        Provision synthetic data for a tenant company_id.
        Use this to seed dashboard data for a company whose API key
        was created before their real connector was set up.

        Admin key required.
        """
        try:
            summary = provision_tenant(
                company_id=req.company_id,
                company_name=req.company_name,
                num_customers=req.num_customers,
            )
            return summary
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Provision failed: {e}")

    @app.post("/admin/train", tags=["Admin"])
    def admin_train_for_tenant(
        company_id: str,
        _admin:     dict = Depends(require_admin),
    ):
        """
        Run ML training + inference for a specific tenant.
        Call this after /admin/provision to populate ml_predictions.
        Admin key required.
        """
        try:
            from models.orchestrator import train_all, run_inference_and_store
            arts  = train_all(verbose=False, company_id=company_id)
            preds = run_inference_and_store(company_id=company_id, verbose=False)
            return {
                "models_trained":    list(arts.keys()),
                "customers_scored":  len(preds),
                "company_id":        company_id,
                "timestamp":         datetime.now().isoformat(),
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Training failed: {e}")

    # NOTE: /admin/reset-key and /admin/debug-key have been permanently removed.
    # These were unauthenticated backdoor routes (SEC-NEW-001). Any emergency
    # key rotation must be done directly via the Railway PostgreSQL console.

    # ── EZER Billing (Razorpay — EZER's own subscriptions) ───────────────────
    # Razorpay works in India and globally (140+ currencies).
    # Required Railway env vars:
    #   EZER_RAZORPAY_KEY_ID      — Razorpay Key ID  (rzp_live_...)
    #   EZER_RAZORPAY_KEY_SECRET  — Razorpay Key Secret
    #   EZER_RAZORPAY_PLAN_STARTER  — Razorpay Plan ID for Starter  (~$49/mo)
    #   EZER_RAZORPAY_PLAN_GROWTH   — Razorpay Plan ID for Growth   (~$149/mo)
    #   EZER_RAZORPAY_PLAN_SCALE    — Razorpay Plan ID for Scale    (~$399/mo)
    #
    # Flow:
    #   1. POST /billing/checkout  → creates Razorpay Subscription, returns sub_id + key_id
    #   2. Frontend opens Razorpay checkout modal using sub_id
    #   3. On payment, Razorpay calls POST /billing/webhook with signature
    #   4. Webhook verifies HMAC-SHA256 and upgrades company plan in DB
    #
    # Create plans in Razorpay dashboard → Subscriptions → Plans (INR or USD).
    # Set webhook URL: https://ezer-production.up.railway.app/billing/webhook
    # Set webhook secret as EZER_RAZORPAY_WEBHOOK_SECRET in Railway.

    class BillingCheckoutRequest(BaseModel):
        plan:        str  # starter | growth | scale
        customer_name:  str = ""
        customer_email: str = ""
        customer_contact: str = ""

    @app.post("/billing/checkout", tags=["Billing"])
    def create_checkout_session(
        req:     BillingCheckoutRequest,
        company: dict = Depends(require_auth),
    ):
        """
        Create a Razorpay Subscription for EZER billing.
        Returns subscription_id and key_id — frontend opens Razorpay modal with these.
        No card data ever touches EZER servers.
        """
        RZP_KEY_ID     = os.environ.get("EZER_RAZORPAY_KEY_ID", "")
        RZP_KEY_SECRET = os.environ.get("EZER_RAZORPAY_KEY_SECRET", "")
        if not RZP_KEY_ID or not RZP_KEY_SECRET:
            raise HTTPException(status_code=503, detail="Billing not configured. Contact support.")

        plan_map = {
            "starter": os.environ.get("EZER_RAZORPAY_PLAN_STARTER", ""),
            "growth":  os.environ.get("EZER_RAZORPAY_PLAN_GROWTH",  ""),
            "scale":   os.environ.get("EZER_RAZORPAY_PLAN_SCALE",   ""),
        }
        plan_id = plan_map.get(req.plan.lower(), "")
        if not plan_id:
            raise HTTPException(status_code=400, detail=f"Unknown plan '{req.plan}'. Choose: starter, growth, scale.")

        try:
            import razorpay as rzp_lib
            client = rzp_lib.Client(auth=(RZP_KEY_ID, RZP_KEY_SECRET))

            # Create or reuse a Razorpay customer
            customer_data: dict = {
                "name":    req.customer_name or company.get("company_name", ""),
                "email":   req.customer_email or company.get("email", ""),
                "contact": req.customer_contact or "",
                "notes":   {"company_id": company["company_id"]},
            }
            rzp_customer = client.customer.create(data=customer_data)
            rzp_customer_id = rzp_customer.get("id", "")

            # Create subscription
            sub = client.subscription.create({
                "plan_id":        plan_id,
                "customer_notify": 1,
                "quantity":        1,
                "total_count":     120,   # max 10 years — effectively perpetual
                "customer_id":     rzp_customer_id,
                "notes": {
                    "company_id":   company["company_id"],
                    "company_name": company["company_name"],
                    "plan":         req.plan,
                },
            })

            return {
                "subscription_id": sub["id"],
                "key_id":          RZP_KEY_ID,
                "plan":            req.plan,
                "status":          sub.get("status"),
                "razorpay_customer_id": rzp_customer_id,
            }
        except Exception as e:
            logger.error("billing checkout error: %s", e)
            raise HTTPException(status_code=500, detail="Could not create subscription.")

    @app.get("/billing/status", tags=["Billing"])
    def billing_status(company: dict = Depends(require_auth)):
        """
        Return the current billing plan and subscription status for this company.
        """
        RZP_KEY_ID     = os.environ.get("EZER_RAZORPAY_KEY_ID", "")
        RZP_KEY_SECRET = os.environ.get("EZER_RAZORPAY_KEY_SECRET", "")
        if not RZP_KEY_ID or not RZP_KEY_SECRET:
            return {"billing_enabled": False, "plan": company.get("plan", "starter")}

        # Plan stored locally in DB (updated by webhook) — no Razorpay API call needed
        return {
            "billing_enabled": True,
            "plan":            company.get("plan", "starter"),
        }

    @app.post("/billing/portal", tags=["Billing"])
    def billing_portal(company: dict = Depends(require_auth)):
        """
        Razorpay has no hosted portal like Stripe.
        Returns a deep-link to the Razorpay customer dashboard and support contact.
        """
        return {
            "portal_url":    "https://dashboard.razorpay.com",
            "support_email": "support@ezerapp.io",
            "message":       "To manage or cancel your subscription, contact support@ezerapp.io or visit the Razorpay dashboard.",
        }

    @app.post("/billing/webhook", tags=["Billing"])
    async def razorpay_webhook(request: Request):
        """
        Razorpay webhook endpoint — handles subscription lifecycle events.
        Set webhook URL in Razorpay dashboard: https://ezer-production.up.railway.app/billing/webhook
        Set EZER_RAZORPAY_WEBHOOK_SECRET in Railway env vars.

        Razorpay signs webhooks with HMAC-SHA256 of the raw body using the webhook secret.
        Header: X-Razorpay-Signature
        """
        import hmac as _hmac
        import hashlib as _hashlib
        import json as _json

        WEBHOOK_SECRET = os.environ.get("EZER_RAZORPAY_WEBHOOK_SECRET", "")

        payload_bytes = await request.body()
        sig = request.headers.get("x-razorpay-signature", "")

        # Verify HMAC-SHA256 signature when secret is configured
        if WEBHOOK_SECRET and sig:
            expected = _hmac.new(
                WEBHOOK_SECRET.encode(),
                payload_bytes,
                _hashlib.sha256,
            ).hexdigest()
            if not _hmac.compare_digest(expected, sig):
                raise HTTPException(status_code=400, detail="Invalid webhook signature.")

        try:
            event = _json.loads(payload_bytes)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON payload.")

        etype = event.get("event", "")
        entity = event.get("payload", {}).get("subscription", {}).get("entity", {})
        notes  = entity.get("notes", {})
        company_id = notes.get("company_id", "")
        plan       = notes.get("plan", "starter")

        if etype in ("subscription.activated", "subscription.charged"):
            if company_id:
                try:
                    conn = get_connection()
                    conn.execute(
                        "UPDATE companies SET plan=? WHERE company_id=?",
                        (plan, company_id)
                    )
                    conn.commit()
                    conn.close()
                    logger.info("billing plan activated: company=%s plan=%s event=%s",
                                company_id, plan, etype)
                except Exception as e:
                    logger.error("failed to update plan for %s: %s", company_id, e)

        elif etype in ("subscription.cancelled", "subscription.expired"):
            if company_id:
                try:
                    conn = get_connection()
                    conn.execute(
                        "UPDATE companies SET plan='cancelled' WHERE company_id=?",
                        (company_id,)
                    )
                    conn.commit()
                    conn.close()
                    logger.info("subscription cancelled: company=%s event=%s", company_id, etype)
                except Exception as e:
                    logger.error("failed to cancel plan for %s: %s", company_id, e)

        return {"received": True, "event": etype}

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
            uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=True)  # nosec B104 — intentional: container/Railway needs binding on all interfaces
        except ImportError:
            print("Install uvicorn:  pip install uvicorn")
