"""
ACIA — Production Entry Point
Used by Railway (and any other host) to start the API server.

Railway sets the PORT environment variable automatically.
We read it and start uvicorn on that port.
"""

import os
import sys
from pathlib import Path

# Ensure project root is on the path
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))


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

    # Run startup sequence
    startup()

    # Get port from Railway (or default to 8000)
    port = int(os.environ.get("PORT", 8000))
    host = os.environ.get("HOST", "0.0.0.0")

    print(f"🌐 Starting ACIA API on {host}:{port}")
    print(f"   Docs: http://localhost:{port}/docs\n")

    uvicorn.run(
        "api:app",
        host=host,
        port=port,
        reload=False,
        workers=1,
        log_level="info",
    )
