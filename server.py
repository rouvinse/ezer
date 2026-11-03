"""
ACIA — Production Entry Point
Used by Railway (and any other host) to start the API server.

Railway sets the PORT environment variable automatically.
We read it and start uvicorn on that port.

Required env vars in production (RAILWAY_ENVIRONMENT=production):
  DATABASE_URL       — PostgreSQL connection string (set by Railway automatically)
  ACIA_ADMIN_KEY     — Fixed admin API key (set once, never change)

Optional:
  PORT               — HTTP port (default: 8000; Railway sets this)
  LOG_LEVEL          — DEBUG | INFO | WARNING | ERROR (default: INFO)
  ANTHROPIC_API_KEY  — Required only for LLM-enhanced agent cycles
"""

import os
import sys
import logging
from pathlib import Path

# Ensure project root is on the path
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
_log = logging.getLogger("acia.server")


def _check_env() -> list[str]:
    """Return a list of startup warnings about missing env vars."""
    warnings: list[str] = []
    is_prod = os.environ.get("RAILWAY_ENVIRONMENT") == "production"

    if is_prod:
        if not os.environ.get("DATABASE_URL"):
            warnings.append("DATABASE_URL not set — Railway should inject this automatically via the PostgreSQL plugin")
        if not os.environ.get("ACIA_ADMIN_KEY"):
            warnings.append("ACIA_ADMIN_KEY not set — a random admin key will be generated each boot (set a fixed value so it persists across deploys)")

    return warnings


def startup():
    """
    Run on first boot:
    1. Generate synthetic data if DB is empty
    2. Train ML models
    3. Run first agent cycle
    4. Bootstrap admin API key
    """
    print("🚀 ACIA — Production startup sequence")

    from utils.db import get_connection, DB_ENGINE
    print(f"   Database engine : {DB_ENGINE.upper()}")

    # Check if DB has data
    try:
        conn   = get_connection()
        count  = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
        conn.close()
    except Exception:
        count = 0

    if count == 0:
        print("   First boot — creating schema only...")
        try:
            from utils.db import get_schema
            conn2 = get_connection()
            conn2.executescript(get_schema())
            conn2.commit()
            conn2.close()
            print("   ✅ Schema created")
        except Exception as e:
            print(f"   ⚠️  Schema error: {e}")
        print("   ℹ️  Use POST /tenants/{id}/provision to load test data")
    else:
        print(f"   ✅ Database ready ({count} customers)")
        try:
            from models.orchestrator import train_all, run_inference_and_store
            train_all(verbose=False)
            run_inference_and_store(verbose=False)
            print("   ✅ Models ready")
        except Exception as e:
            print(f"   ⚠️  Model training skipped: {e}")

    # Always ensure admin key is valid in DB
    try:
        from utils.auth import ensure_admin_key_in_db
        ensure_admin_key_in_db()
    except Exception as e:
        print(f"   ⚠️  Admin key setup: {e}")

    # Always ensure auth + approval schemas exist
    from utils.auth import ensure_auth_schema, bootstrap_admin_key
    from utils.tenancy import run_tenant_migration
    from actions.approval import ensure_approval_schema

    ensure_auth_schema()
    bootstrap_admin_key()
    run_tenant_migration()
    ensure_approval_schema()

    print("   ✅ Startup complete — starting API server\n")


if __name__ == "__main__":
    import uvicorn

    # Check env vars and emit warnings (never hard-fail — DB may still connect)
    for warning in _check_env():
        _log.warning("⚠️  %s", warning)

    # Run startup sequence
    startup()

    # Get port from Railway (or default to 8000)
    port = int(os.environ.get("PORT", 8000))
    host = os.environ.get("HOST", "0.0.0.0")
    log_level = os.environ.get("LOG_LEVEL", "info").lower()

    _log.info("🌐 Starting ACIA API on %s:%s", host, port)

    uvicorn.run(
        "api:app",
        host=host,
        port=port,
        reload=False,
        workers=1,
        log_level=log_level,
        access_log=False,   # handled by RequestContextMiddleware
        timeout_graceful_shutdown=30,
    )
