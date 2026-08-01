"""
ACIA — Multi-Tenancy Module
Ensures every company (tenant) sees only their own data.

How it works:
  1. Every table gets a company_id column
  2. All queries are automatically scoped: WHERE company_id = ?
  3. The current tenant is set via set_tenant(company_id)
  4. A TenantContext object is passed through the pipeline so
     every layer knows which company's data to read/write

Key design decisions:
  - Single database, multiple tenants (simplest approach for early stage)
  - company_id is a UUID matching the companies table in auth
  - Default company_id = 'default' for backward compatibility
    with existing synthetic data
  - All existing data is migrated to 'default' tenant automatically

Migration approach:
  - ADD COLUMN company_id to all 7 tables (non-destructive)
  - UPDATE existing rows to company_id = 'default'
  - CREATE indexes on company_id for query performance
  - No data loss, fully reversible
"""

from __future__ import annotations
import uuid
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass
from contextlib import contextmanager
from typing import Optional

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.db import get_connection, read_sql

# ── Default tenant ────────────────────────────────────────────────────────────
# All synthetic/dev data lives under this tenant
DEFAULT_TENANT_ID = "default"

# Tables that need company_id scoping
TENANT_TABLES = [
    "customers",
    "transactions",
    "engagement_events",
    "support_tickets",
    "email_log",
    "agent_actions",
    "ml_predictions",
]


# ── Tenant context ─────────────────────────────────────────────────────────────

@dataclass
class TenantContext:
    """
    Carries the current tenant identity through the pipeline.
    Pass this object to any function that reads or writes data.
    """
    company_id:   str
    company_name: str
    plan:         str = "starter"

    def __str__(self):
        return f"TenantContext({self.company_name}, {self.company_id[:8]}...)"


# Thread-local-style current tenant (simple global for now)
_current_tenant: Optional[TenantContext] = None


def set_tenant(company_id: str, company_name: str = "", plan: str = "starter"):
    """Set the current active tenant for this execution context."""
    global _current_tenant
    _current_tenant = TenantContext(
        company_id=company_id,
        company_name=company_name,
        plan=plan,
    )


def get_tenant() -> TenantContext:
    """Get the current active tenant. Falls back to default."""
    if _current_tenant:
        return _current_tenant
    return TenantContext(
        company_id=DEFAULT_TENANT_ID,
        company_name="Default",
        plan="starter",
    )


def clear_tenant():
    """Clear the current tenant (reset to default)."""
    global _current_tenant
    _current_tenant = None


@contextmanager
def tenant_context(company_id: str, company_name: str = "", plan: str = "starter"):
    """
    Context manager for scoped tenant operations.

    Usage:
        with tenant_context("company-uuid", "Acme Corp"):
            run_agent_cycle()   # only touches Acme's data
    """
    global _current_tenant
    previous = _current_tenant
    set_tenant(company_id, company_name, plan)
    try:
        yield get_tenant()
    finally:
        _current_tenant = previous


# ── Schema migration ───────────────────────────────────────────────────────────

def add_company_id_columns():
    """
    Add company_id column to all tenant tables.
    Safe to run multiple times — skips if column already exists.
    """
    conn = get_connection()

    for table in TENANT_TABLES:
        try:
            # Check if column already exists
            result = conn.execute(f"PRAGMA table_info({table})").fetchall()
            col_names = [r[1] for r in result]

            if "company_id" not in col_names:
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN company_id TEXT DEFAULT '{DEFAULT_TENANT_ID}'"
                )
                print(f"  ✅ Added company_id to {table}")
            else:
                print(f"  ⏭  {table} already has company_id")
        except Exception as e:
            print(f"  ⚠️  {table}: {e}")

    conn.commit()
    conn.close()


def backfill_default_tenant():
    """
    Set company_id = 'default' on all rows that have NULL company_id.
    Runs after add_company_id_columns() to handle existing data.
    """
    conn = get_connection()
    total_updated = 0

    for table in TENANT_TABLES:
        try:
            result = conn.execute(
                f"UPDATE {table} SET company_id = ? WHERE company_id IS NULL",
                (DEFAULT_TENANT_ID,)
            )
            # Count rows updated via SELECT
            count = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE company_id = ?",
                (DEFAULT_TENANT_ID,)
            ).fetchone()[0]
            total_updated += count
            print(f"  ✅ {table:25s}: {count:,} rows on default tenant")
        except Exception as e:
            print(f"  ⚠️  {table}: {e}")

    conn.commit()
    conn.close()
    return total_updated


def create_tenant_indexes():
    """
    Create indexes on company_id for fast tenant-scoped queries.
    Critical for performance once you have multiple tenants.
    """
    conn = get_connection()

    indexes = [
        ("idx_customers_tenant",         "customers",         "company_id"),
        ("idx_transactions_tenant",      "transactions",      "company_id"),
        ("idx_events_tenant",            "engagement_events", "company_id"),
        ("idx_tickets_tenant",           "support_tickets",   "company_id"),
        ("idx_email_log_tenant",         "email_log",         "company_id"),
        ("idx_agent_actions_tenant",     "agent_actions",     "company_id"),
        ("idx_ml_predictions_tenant",    "ml_predictions",    "company_id"),
        # Composite indexes for common query patterns
        ("idx_customers_tenant_churned", "customers",         "company_id, is_churned"),
        ("idx_actions_tenant_status",    "agent_actions",     "company_id, status"),
        ("idx_preds_tenant_churn",       "ml_predictions",    "company_id, churn_score"),
    ]

    for idx_name, table, cols in indexes:
        try:
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table} ({cols})"
            )
            print(f"  ✅ Index: {idx_name}")
        except Exception as e:
            print(f"  ⚠️  {idx_name}: {e}")

    conn.commit()
    conn.close()


def run_tenant_migration():
    """
    Full migration: add columns + backfill + indexes.
    Run this once to enable multi-tenancy on an existing database.
    """
    print("\n── Multi-Tenancy Migration ───────────────────────────")
    print("\n[1/3] Adding company_id columns...")
    add_company_id_columns()

    print("\n[2/3] Backfilling existing data to default tenant...")
    total = backfill_default_tenant()
    print(f"       Total rows on default tenant: {total:,}")

    print("\n[3/3] Creating performance indexes...")
    create_tenant_indexes()

    print("\n✅ Migration complete. Multi-tenancy is active.\n")


# ── Tenant-scoped data operations ─────────────────────────────────────────────

def get_tenant_stats(company_id: str) -> dict:
    """Return row counts per table for a specific tenant."""
    conn = get_connection()
    stats = {"company_id": company_id}

    for table in TENANT_TABLES:
        try:
            count = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE company_id = ?",
                (company_id,)
            ).fetchone()[0]
            stats[table] = count
        except Exception:
            stats[table] = 0

    conn.close()
    return stats


def list_tenants() -> list[dict]:
    """List all tenants with their data counts."""
    try:
        df = read_sql(
            "SELECT company_id, COUNT(*) as customer_count "
            "FROM customers GROUP BY company_id ORDER BY customer_count DESC"
        )
        return df.to_dict("records")
    except Exception:
        return []


def delete_tenant_data(company_id: str, confirm: bool = False) -> dict:
    """
    Delete ALL data for a specific tenant.
    Requires confirm=True to prevent accidents.
    """
    if not confirm:
        raise ValueError(
            "You must pass confirm=True to delete tenant data. "
            "This action is irreversible."
        )

    if company_id == DEFAULT_TENANT_ID:
        raise ValueError("Cannot delete the default tenant.")

    conn = get_connection()
    deleted = {}

    # Delete in reverse dependency order
    for table in reversed(TENANT_TABLES):
        try:
            result = conn.execute(
                f"DELETE FROM {table} WHERE company_id = ?",
                (company_id,)
            )
            deleted[table] = result.rowcount if hasattr(result, 'rowcount') else 0
        except Exception as e:
            deleted[table] = f"error: {e}"

    conn.commit()
    conn.close()
    return deleted


# ── Tenant-aware generate ─────────────────────────────────────────────────────

def provision_tenant(
    company_id:   str,
    company_name: str,
    num_customers: int = 100,
    seed: int | None = None,
) -> dict:
    """
    Provision a new tenant with synthetic data.
    Used for demos, testing, and onboarding new customers
    before their real data connector is set up.

    Returns summary of what was created.
    """
    import random
    if seed is not None:
        random.seed(seed)

    print(f"\n── Provisioning tenant: {company_name} ──────────────")

    # Import generators from data module
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from data.generate import (
        gen_customers, gen_transactions, gen_events,
        gen_tickets, gen_emails, _insert_many
    )
    from utils.db import DB_ENGINE

    # Generate synthetic data
    customers = gen_customers(num_customers)
    txns      = gen_transactions(customers)
    events    = gen_events(customers)
    tickets   = gen_tickets(customers)
    emails    = gen_emails(customers)

    conn = get_connection()

    # Tag all rows with this tenant's company_id
    def tag(rows: list[dict]) -> list[dict]:
        for r in rows:
            r["company_id"] = company_id
        return rows

    customers_t = tag(customers)
    txns_t      = tag(txns)
    events_t    = tag(events)
    tickets_t   = tag(tickets)
    emails_t    = tag(emails)

    # Insert with company_id column
    _insert_many(conn, "customers", [
        "customer_id","name","email","company","industry","plan","mrr",
        "signup_date","country","employee_count","health_score","nps_score",
        "is_churned","churn_date","created_at","company_id"
    ], customers_t)

    _insert_many(conn, "transactions", [
        "txn_id","customer_id","amount","currency","txn_type",
        "product_id","status","txn_date","company_id"
    ], txns_t)

    _insert_many(conn, "engagement_events", [
        "event_id","customer_id","event_type","channel",
        "session_duration","event_date","metadata","company_id"
    ], events_t)

    _insert_many(conn, "support_tickets", [
        "ticket_id","customer_id","topic","status","priority",
        "sentiment_score","opened_at","resolved_at","company_id"
    ], tickets_t)

    _insert_many(conn, "email_log", [
        "email_id","customer_id","email_type","subject",
        "opened","clicked","sent_at","company_id"
    ], emails_t)

    conn.commit()
    conn.close()

    summary = {
        "company_id":   company_id,
        "company_name": company_name,
        "customers":    len(customers),
        "transactions": len(txns),
        "events":       len(events),
        "tickets":      len(tickets),
        "emails":       len(emails),
        "provisioned_at": datetime.now().isoformat(),
    }

    print(f"  ✅ {len(customers)} customers, {len(txns)} txns, {len(events)} events")
    return summary


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="ACIA Multi-Tenancy Management")
    sub    = parser.add_subparsers(dest="command")

    sub.add_parser("migrate",  help="Run full multi-tenancy migration")
    sub.add_parser("list",     help="List all tenants")

    p_stats = sub.add_parser("stats", help="Show data stats for a tenant")
    p_stats.add_argument("company_id")

    p_prov = sub.add_parser("provision", help="Provision a new tenant with synthetic data")
    p_prov.add_argument("--company-id",   required=True)
    p_prov.add_argument("--company-name", required=True)
    p_prov.add_argument("--customers",    type=int, default=100)

    p_del = sub.add_parser("delete", help="Delete all data for a tenant")
    p_del.add_argument("company_id")
    p_del.add_argument("--confirm", action="store_true")

    args = parser.parse_args()

    if args.command == "migrate":
        run_tenant_migration()

    elif args.command == "list":
        tenants = list_tenants()
        if not tenants:
            print("No tenants found.")
        else:
            print(f"\n{'Company ID':38} {'Customers':>10}")
            print("─" * 52)
            for t in tenants:
                print(f"  {t['company_id']:36} {t['customer_count']:>10,}")

    elif args.command == "stats":
        stats = get_tenant_stats(args.company_id)
        print(f"\nTenant: {args.company_id}")
        for table, count in stats.items():
            if table != "company_id":
                print(f"  {table:25s}: {count:,}")

    elif args.command == "provision":
        summary = provision_tenant(
            args.company_id,
            args.company_name,
            args.customers,
        )
        print(f"\n✅ Tenant provisioned: {summary}")

    elif args.command == "delete":
        if not args.confirm:
            print("⚠️  Add --confirm to delete tenant data. This is irreversible.")
        else:
            deleted = delete_tenant_data(args.company_id, confirm=True)
            print(f"✅ Deleted: {deleted}")

    else:
        parser.print_help()
