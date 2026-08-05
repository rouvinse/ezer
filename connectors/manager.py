"""
EZER — Connector Manager
========================
Central registry for all data connectors.
Handles discovery, configuration checking, and unified sync.

Available connectors:
  - stripe     → Stripe payments and subscriptions
  - razorpay   → Razorpay payments (India)
  - hubspot    → HubSpot CRM contacts and deals

Usage:
  python connectors/manager.py --list
  python connectors/manager.py --check
  python connectors/manager.py --sync stripe --company-id YOUR_ID
  python connectors/manager.py --sync all --company-id YOUR_ID
"""

from __future__ import annotations
import os
import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Connector registry ────────────────────────────────────────────────────────

CONNECTORS = {
    "stripe": {
        "name":        "Stripe",
        "description": "Payment subscriptions and customer billing",
        "env_vars":    ["STRIPE_SECRET_KEY"],
        "module":      "connectors.stripe_connector",
        "sync_fn":     "sync_stripe",
        "test_fn":     "test_connection",
        "ideal_for":   "Global SaaS companies using Stripe for billing",
    },
    "razorpay": {
        "name":        "Razorpay",
        "description": "Indian payment gateway — payments and subscriptions",
        "env_vars":    ["RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET"],
        "module":      "connectors.razorpay_connector",
        "sync_fn":     "sync_razorpay",
        "test_fn":     "test_connection",
        "ideal_for":   "Indian SaaS companies using Razorpay for billing",
    },
    "hubspot": {
        "name":        "HubSpot",
        "description": "CRM contacts, companies, deals, and engagement",
        "env_vars":    ["HUBSPOT_ACCESS_TOKEN"],
        "module":      "connectors.hubspot_connector",
        "sync_fn":     "sync_hubspot",
        "test_fn":     "test_connection",
        "ideal_for":   "Companies using HubSpot as their primary CRM",
    },
}

# Connectors coming soon
COMING_SOON = [
    "Intercom — Customer support and messaging",
    "Freshdesk — Support ticket management",
    "Mixpanel — Product usage analytics",
    "Amplitude — User behaviour analytics",
    "Chargebee — Subscription management",
    "Salesforce — Enterprise CRM",
    "Pipedrive — Sales CRM",
    "Zendesk — Customer support",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def check_connector(key: str) -> dict:
    """Check if a connector is configured (all env vars present)."""
    connector = CONNECTORS[key]
    missing   = [v for v in connector["env_vars"] if not os.environ.get(v)]
    return {
        "key":         key,
        "name":        connector["name"],
        "configured":  len(missing) == 0,
        "missing_vars": missing,
    }


def list_connectors():
    """Print all available connectors and their status."""
    print("\n── EZER Connectors ──────────────────────────────────")
    print()
    for key, info in CONNECTORS.items():
        status = check_connector(key)
        icon   = "✅" if status["configured"] else "⚠️ "
        print(f"  {icon} {info['name']:12} — {info['description']}")
        if not status["configured"]:
            print(f"       Missing: {', '.join(status['missing_vars'])}")
    print()
    print("── Coming Soon ──────────────────────────────────────")
    for item in COMING_SOON:
        print(f"  ⏳ {item}")
    print(f"────────────────────────────────────────────────────\n")


def check_all():
    """Check configuration status of all connectors."""
    print("\n── Connector Configuration Check ────────────────────")
    for key in CONNECTORS:
        status = check_connector(key)
        if status["configured"]:
            print(f"  ✅ {status['name']:12} — configured")
        else:
            print(f"  ❌ {status['name']:12} — missing: {', '.join(status['missing_vars'])}")
    print(f"────────────────────────────────────────────────────\n")


def sync_connector(key: str, company_id: str) -> dict:
    """Run sync for a specific connector."""
    if key not in CONNECTORS:
        raise ValueError(f"Unknown connector: {key}. Available: {list(CONNECTORS.keys())}")

    status = check_connector(key)
    if not status["configured"]:
        raise ValueError(
            f"{status['name']} not configured. "
            f"Set: {', '.join(status['missing_vars'])}"
        )

    import importlib
    module  = importlib.import_module(CONNECTORS[key]["module"])
    sync_fn = getattr(module, CONNECTORS[key]["sync_fn"])
    return sync_fn(company_id=company_id)


def sync_all(company_id: str) -> dict:
    """Run all configured connectors."""
    results = {}
    for key in CONNECTORS:
        status = check_connector(key)
        if status["configured"]:
            print(f"\n▶ Syncing {status['name']}...")
            try:
                results[key] = sync_connector(key, company_id)
            except Exception as e:
                results[key] = {"error": str(e)}
                print(f"  ❌ {status['name']} sync failed: {e}")
        else:
            print(f"  ⏭ Skipping {status['name']} — not configured")
    return results


def add_all_connector_endpoints(app, require_auth):
    """
    Register all connector API endpoints with the FastAPI app.
    Call this from api.py to expose all connectors via REST.
    """
    from connectors.stripe_connector    import add_stripe_endpoints
    from connectors.razorpay_connector  import add_razorpay_endpoints
    from connectors.hubspot_connector   import add_hubspot_endpoints
    from fastapi import Depends
    from pydantic import BaseModel

    add_stripe_endpoints(app, require_auth)
    add_razorpay_endpoints(app, require_auth)
    add_hubspot_endpoints(app, require_auth)

    # Unified sync endpoint
    class UnifiedSyncRequest(BaseModel):
        connectors: list[str] = ["all"]  # ["stripe", "hubspot"] or ["all"]

    @app.post("/connectors/sync", tags=["Connectors"])
    def sync_all_connectors(
        req:     UnifiedSyncRequest,
        company: dict = Depends(require_auth),
    ):
        """
        Sync one or all configured connectors.
        Pass connectors=["all"] to sync everything configured.
        Pass connectors=["stripe", "hubspot"] to sync specific ones.
        """
        results = {}
        keys = list(CONNECTORS.keys()) if "all" in req.connectors else req.connectors

        for key in keys:
            if key not in CONNECTORS:
                results[key] = {"error": f"Unknown connector: {key}"}
                continue
            status = check_connector(key)
            if not status["configured"]:
                results[key] = {"skipped": f"Not configured — set {status['missing_vars']}"}
                continue
            try:
                results[key] = sync_connector(key, company["company_id"])
            except Exception as e:
                results[key] = {"error": str(e)}

        return results

    @app.get("/connectors", tags=["Connectors"])
    def list_all_connectors(company: dict = Depends(require_auth)):
        """List all available connectors and their configuration status."""
        return {
            "available": [
                {
                    "key":         key,
                    "name":        info["name"],
                    "description": info["description"],
                    "configured":  check_connector(key)["configured"],
                    "ideal_for":   info["ideal_for"],
                }
                for key, info in CONNECTORS.items()
            ],
            "coming_soon": COMING_SOON,
        }


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="EZER — Connector Manager")
    parser.add_argument("--list",       action="store_true", help="List all connectors")
    parser.add_argument("--check",      action="store_true", help="Check configuration")
    parser.add_argument("--sync",       metavar="CONNECTOR",  help="Sync a connector (or 'all')")
    parser.add_argument("--company-id", default="default",    help="Company/tenant ID")
    args = parser.parse_args()

    if args.list:
        list_connectors()

    elif args.check:
        check_all()

    elif args.sync:
        if args.sync == "all":
            sync_all(args.company_id)
        else:
            sync_connector(args.sync, args.company_id)

    else:
        parser.print_help()
