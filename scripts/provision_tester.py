#!/usr/bin/env python3
"""
EZER — One-time Railway setup script.

Run this script on Railway (via Railway CLI or console) to:
  1. Provision synthetic data for the Tester-001 company
  2. Run ML training for that company
  3. Trigger an agent cycle so the approval queue populates

This fixes the "dashboard shows all zeros" issue caused by company_id mismatch
between the 500 synthetic customers (tagged 'default') and the Tester-001
API key (UUID company_id).

Usage (Railway CLI):
    railway run python scripts/provision_tester.py

Requirements:
    - DB_ENGINE=postgresql + DATABASE_URL must be set (auto-set on Railway)
    - ACIA_ADMIN_KEY must be set in Railway environment variables
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

TESTER_COMPANY_ID = "bacabdfa-d035-46bc-90ce-bbe4b3e386e3"
TESTER_COMPANY_NAME = "Tester-001"
NUM_CUSTOMERS = 100

def main():
    print("=" * 60)
    print("  EZER — Railway Tenant Provisioning")
    print("=" * 60)

    # 1. Ensure auth/approval schema exists
    print("\n[1/4] Ensuring database schema...")
    from utils.auth import ensure_auth_schema
    from actions.approval import ensure_approval_schema
    ensure_auth_schema()
    ensure_approval_schema()
    print("  ✅ Schema ready")

    # 2. Provision synthetic data for Tester-001
    print(f"\n[2/4] Provisioning {NUM_CUSTOMERS} customers for {TESTER_COMPANY_NAME}...")
    from utils.tenancy import provision_tenant, get_tenant_stats
    summary = provision_tenant(
        company_id=TESTER_COMPANY_ID,
        company_name=TESTER_COMPANY_NAME,
        num_customers=NUM_CUSTOMERS,
    )
    print(f"  ✅ {summary['customers']} customers created")
    print(f"  ✅ {summary['transactions']} transactions created")
    print(f"  ✅ {summary['events']} engagement events created")

    # 3. Train ML models + run inference
    print(f"\n[3/4] Training ML models for {TESTER_COMPANY_NAME}...")
    from models.orchestrator import train_all, run_inference_and_store
    arts  = train_all(verbose=True, company_id=TESTER_COMPANY_ID)
    preds = run_inference_and_store(company_id=TESTER_COMPANY_ID, verbose=True)
    print(f"  ✅ Models trained: {list(arts.keys())}")
    print(f"  ✅ {len(preds)} customers scored")

    # 4. Run one agent cycle to populate approval queue
    print(f"\n[4/4] Running agent cycle to generate actions...")
    from agent.runner import run_agent_cycle
    cycle = run_agent_cycle(
        use_llm=False,
        company_id=TESTER_COMPANY_ID,
        verbose=True,
        dry_run=False,
    )
    print(f"  ✅ {cycle['queued_actions']} actions queued")
    print(f"  ✅ {cycle['critical']} critical, {cycle['high']} high priority")

    # 5. Print final stats
    print(f"\n{'='*60}")
    stats = get_tenant_stats(TESTER_COMPANY_ID)
    print(f"  Final tenant stats for {TESTER_COMPANY_NAME}:")
    for table, count in stats.items():
        if table != "company_id":
            print(f"    {table:25s}: {count:,}")
    print(f"\n  ✅ Tester-001 is fully provisioned.")
    print(f"  Dashboard at https://ezerapp.netlify.app should now show real data.")
    print(f"{'='*60}\n")

if __name__ == "__main__":
    main()
