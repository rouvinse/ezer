"""
EZER — Stripe Connector
=======================
Pulls real customer and revenue data from a Stripe account
and maps it into EZER's database schema.

What it pulls:
  - Customers (name, email, plan, MRR, signup date, country)
  - Subscriptions (plan, status, MRR, renewal dates)
  - Invoices (transactions — payments, failures, refunds)
  - Charges (payment history)

What it maps to:
  - customers table
  - transactions table

What it does NOT pull (needs other connectors):
  - Support tickets → Intercom / Freshdesk connector
  - Email engagement → SendGrid / Mailchimp connector
  - Product usage / logins → Mixpanel / Amplitude connector

Setup:
  1. Get your Stripe Secret Key from dashboard.stripe.com/apikeys
  2. Set environment variable: STRIPE_SECRET_KEY=sk_live_...
  3. Run: python connectors/stripe_connector.py --sync
  4. Or call sync_stripe(company_id) from your code

Usage:
  python connectors/stripe_connector.py --sync --company-id YOUR_COMPANY_ID
  python connectors/stripe_connector.py --test   # test connection only
  python connectors/stripe_connector.py --status # show what's in DB
"""

from __future__ import annotations
import os
import uuid
import sys
import argparse
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Constants ─────────────────────────────────────────────────────────────────

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")

# Map Stripe plan intervals to readable plan names
PLAN_MAP = {
    "month": {
        1:   "Starter",
        2:   "Starter",
        3:   "Starter",
        6:   "Growth",
        12:  "Scale",
    },
    "year": {
        1: "Scale",
    },
}

# Map Stripe currency to our standard
CURRENCY_MAP = {
    "usd": "USD", "eur": "EUR", "gbp": "GBP",
    "inr": "INR", "sgd": "SGD", "aud": "AUD",
    "cad": "CAD", "jpy": "JPY",
}


# ── Connection ─────────────────────────────────────────────────────────────────

def get_stripe():
    """Get authenticated Stripe client."""
    try:
        import stripe
    except ImportError:
        raise ImportError(
            "stripe not installed. Run: pip install stripe"
        )

    key = STRIPE_SECRET_KEY
    if not key:
        raise ValueError(
            "STRIPE_SECRET_KEY environment variable not set.\n"
            "Get your key from: https://dashboard.stripe.com/apikeys\n"
            "Then run: export STRIPE_SECRET_KEY=sk_live_..."
        )

    stripe.api_key = key
    return stripe


def test_connection() -> bool:
    """Test Stripe connection and show account info."""
    try:
        stripe = get_stripe()
        account = stripe.Account.retrieve()
        print(f"\n✅ Stripe connected")
        print(f"   Account : {account.get('business_profile', {}).get('name', 'Unknown')}")
        print(f"   Email   : {account.get('email', 'Unknown')}")
        print(f"   Country : {account.get('country', 'Unknown')}")
        print(f"   Mode    : {'LIVE' if not STRIPE_SECRET_KEY.startswith('sk_test') else 'TEST'}\n")
        return True
    except Exception as e:
        print(f"\n❌ Stripe connection failed: {e}\n")
        return False


# ── Data fetchers ─────────────────────────────────────────────────────────────

def fetch_stripe_customers(stripe, limit: int = 100) -> list[dict]:
    """Fetch all customers from Stripe with pagination."""
    customers = []
    starting_after = None

    print("  Fetching customers from Stripe...")
    while True:
        params = {"limit": limit, "expand": ["data.subscriptions"]}
        if starting_after:
            params["starting_after"] = starting_after

        batch = stripe.Customer.list(**params)
        customers.extend(batch.data)

        if not batch.has_more:
            break
        starting_after = batch.data[-1].id

    print(f"  Found {len(customers)} customers in Stripe")
    return customers


def fetch_stripe_invoices(stripe, customer_id: str) -> list[dict]:
    """Fetch all invoices for a specific customer."""
    invoices = []
    starting_after = None

    while True:
        params = {
            "customer": customer_id,
            "limit": 100,
        }
        if starting_after:
            params["starting_after"] = starting_after

        batch = stripe.Invoice.list(**params)
        invoices.extend(batch.data)

        if not batch.has_more:
            break
        starting_after = batch.data[-1].id

    return invoices


# ── Data mappers ───────────────────────────────────────────────────────────────

def get_mrr_from_subscription(subscription) -> float:
    """Calculate MRR from a Stripe subscription object."""
    if not subscription or subscription.get("status") not in ["active", "trialing"]:
        return 0.0

    items = subscription.get("items", {}).get("data", [])
    if not items:
        return 0.0

    total = 0.0
    for item in items:
        price  = item.get("price", {})
        amount = price.get("unit_amount", 0) or 0
        curr   = price.get("currency", "usd")
        interval = price.get("recurring", {}).get("interval", "month")
        interval_count = price.get("recurring", {}).get("interval_count", 1)

        # Convert to monthly
        monthly = amount / 100  # Stripe amounts are in cents
        if interval == "year":
            monthly = monthly / 12
        elif interval == "week":
            monthly = monthly * 4.33
        elif interval == "day":
            monthly = monthly * 30

        total += monthly

    return round(total, 2)


def get_plan_name(subscription) -> str:
    """Derive a human-readable plan name from a Stripe subscription."""
    if not subscription:
        return "Free"

    items = subscription.get("items", {}).get("data", [])
    if not items:
        return "Free"

    price = items[0].get("price", {})
    nickname = price.get("nickname") or price.get("product", {})

    # If Stripe has a product name, use it
    if isinstance(nickname, str) and nickname:
        return nickname

    # Otherwise derive from interval and amount
    interval = price.get("recurring", {}).get("interval", "month")
    mrr = get_mrr_from_subscription(subscription)

    if mrr == 0:
        return "Free"
    elif mrr < 100:
        return "Starter"
    elif mrr < 500:
        return "Growth"
    else:
        return "Scale"


def map_customer(stripe_customer: dict, company_id: str) -> dict:
    """Map a Stripe customer to EZER's customer schema."""
    now = datetime.now(timezone.utc).isoformat()

    # Get active subscription
    subs = stripe_customer.get("subscriptions", {})
    sub_data = subs.get("data", []) if subs else []
    active_sub = next(
        (s for s in sub_data if s.get("status") in ["active", "trialing"]),
        None
    )

    # Core fields
    mrr          = get_mrr_from_subscription(active_sub) if active_sub else 0.0
    plan         = get_plan_name(active_sub)
    is_churned   = 1 if (not active_sub and sub_data) else 0
    churn_date   = None

    # Get churn date from canceled subscription
    if is_churned:
        canceled = next(
            (s for s in sub_data if s.get("status") == "canceled"),
            None
        )
        if canceled and canceled.get("canceled_at"):
            churn_date = datetime.fromtimestamp(
                canceled["canceled_at"], tz=timezone.utc
            ).strftime("%Y-%m-%d")

    # Signup date
    created_ts   = stripe_customer.get("created", 0)
    signup_date  = datetime.fromtimestamp(
        created_ts, tz=timezone.utc
    ).strftime("%Y-%m-%d") if created_ts else now[:10]

    # Name
    name = stripe_customer.get("name") or stripe_customer.get("email", "Unknown")

    # Health score — start at 70, adjust based on subscription status
    health_score = 70.0
    if active_sub:
        status = active_sub.get("status", "")
        if status == "active":
            health_score = 75.0
        elif status == "trialing":
            health_score = 65.0
        elif status == "past_due":
            health_score = 30.0
    elif is_churned:
        health_score = 10.0

    return {
        "customer_id":    str(uuid.uuid4()),
        "stripe_id":      stripe_customer.get("id", ""),
        "name":           name[:200] if name else "Unknown",
        "email":          stripe_customer.get("email", "")[:200],
        "company":        stripe_customer.get("description", "") or name,
        "industry":       "Unknown",
        "plan":           plan,
        "mrr":            mrr,
        "signup_date":    signup_date,
        "country":        stripe_customer.get("address", {}).get("country", "Unknown") if stripe_customer.get("address") else "Unknown",
        "employee_count": 0,
        "health_score":   health_score,
        "nps_score":      None,
        "is_churned":     is_churned,
        "churn_date":     churn_date,
        "created_at":     now,
        "company_id":     company_id,
    }


def map_invoice(invoice: dict, customer_id: str, company_id: str) -> dict | None:
    """Map a Stripe invoice to EZER's transactions schema."""
    if not invoice.get("id"):
        return None

    amount   = (invoice.get("amount_paid") or invoice.get("total") or 0) / 100
    currency = CURRENCY_MAP.get(invoice.get("currency", "usd"), "USD")

    # Transaction type
    if invoice.get("billing_reason") == "subscription_create":
        txn_type = "subscription_new"
    elif invoice.get("billing_reason") == "subscription_cycle":
        txn_type = "subscription_renewal"
    elif invoice.get("billing_reason") == "subscription_update":
        txn_type = "subscription_upgrade"
    elif amount < 0:
        txn_type = "refund"
    else:
        txn_type = "payment"

    # Status
    stripe_status = invoice.get("status", "")
    if stripe_status == "paid":
        status = "completed"
    elif stripe_status == "open":
        status = "pending"
    elif stripe_status == "void":
        status = "refunded"
    else:
        status = "failed"

    # Date
    ts = invoice.get("created", 0)
    txn_date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
        "%Y-%m-%d"
    ) if ts else datetime.now().strftime("%Y-%m-%d")

    return {
        "txn_id":      str(uuid.uuid4()),
        "stripe_id":   invoice.get("id", ""),
        "customer_id": customer_id,
        "amount":      amount,
        "currency":    currency,
        "txn_type":    txn_type,
        "product_id":  invoice.get("subscription", "") or "",
        "status":      status,
        "txn_date":    txn_date,
        "company_id":  company_id,
    }


# ── Database writer ────────────────────────────────────────────────────────────

def write_to_db(
    customers:    list[dict],
    transactions: list[dict],
    company_id:   str,
):
    """Write mapped Stripe data to EZER's database."""
    from utils.db import get_connection, DB_ENGINE

    conn = get_connection()

    # Clear existing Stripe data for this company (fresh sync)
    print("  Clearing existing data for fresh sync...")
    conn.execute(
        "DELETE FROM transactions WHERE company_id = ?", (company_id,)
    )
    conn.execute(
        "DELETE FROM customers WHERE company_id = ?", (company_id,)
    )
    conn.commit()

    # Insert customers
    print(f"  Writing {len(customers)} customers...")
    customer_cols = [
        "customer_id", "name", "email", "company", "industry",
        "plan", "mrr", "signup_date", "country", "employee_count",
        "health_score", "nps_score", "is_churned", "churn_date",
        "created_at", "company_id"
    ]

    if DB_ENGINE == "postgresql":
        placeholders = ", ".join(["%s"] * len(customer_cols))
        sql = f"INSERT INTO customers ({', '.join(customer_cols)}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"
        data = [tuple(c.get(col) for col in customer_cols) for c in customers]
    else:
        placeholders = ", ".join([f":{c}" for c in customer_cols])
        sql = f"INSERT OR REPLACE INTO customers ({', '.join(customer_cols)}) VALUES ({placeholders})"
        data = customers

    conn.executemany(sql, data)
    conn.commit()

    # Insert transactions
    print(f"  Writing {len(transactions)} transactions...")
    txn_cols = [
        "txn_id", "customer_id", "amount", "currency",
        "txn_type", "product_id", "status", "txn_date", "company_id"
    ]

    if transactions:
        if DB_ENGINE == "postgresql":
            placeholders = ", ".join(["%s"] * len(txn_cols))
            sql = f"INSERT INTO transactions ({', '.join(txn_cols)}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"
            data = [tuple(t.get(col) for col in txn_cols) for t in transactions]
        else:
            placeholders = ", ".join([f":{c}" for c in txn_cols])
            sql = f"INSERT OR REPLACE INTO transactions ({', '.join(txn_cols)}) VALUES ({placeholders})"
            data = transactions

        conn.executemany(sql, data)
        conn.commit()

    conn.close()


# ── Main sync function ─────────────────────────────────────────────────────────

def sync_stripe(
    company_id:  str,
    fetch_invoices: bool = True,
    verbose:     bool = True,
) -> dict:
    """
    Full Stripe → EZER sync.
    Pulls all customers and transactions, maps them, writes to DB.

    Args:
        company_id:     The EZER company/tenant ID to sync data under
        fetch_invoices: Whether to also fetch invoice/transaction history
        verbose:        Print progress

    Returns:
        Summary dict with counts and any errors
    """
    if verbose:
        print(f"\n── Stripe Sync ──────────────────────────────────────")
        print(f"  Company ID : {company_id}")
        print(f"  Mode       : {'LIVE' if 'live' in STRIPE_SECRET_KEY else 'TEST'}")
        print()

    stripe = get_stripe()
    errors = []

    # ── Step 1: Fetch customers ────────────────────────────────────────────────
    if verbose:
        print("[1/3] Fetching customers from Stripe...")
    stripe_customers = fetch_stripe_customers(stripe)

    # ── Step 2: Map customers + fetch invoices ─────────────────────────────────
    if verbose:
        print(f"\n[2/3] Mapping {len(stripe_customers)} customers...")

    mapped_customers = []
    mapped_transactions = []

    for i, sc in enumerate(stripe_customers):
        try:
            customer = map_customer(sc, company_id)
            mapped_customers.append(customer)

            if fetch_invoices:
                invoices = fetch_stripe_invoices(stripe, sc["id"])
                for inv in invoices:
                    txn = map_invoice(inv, customer["customer_id"], company_id)
                    if txn:
                        mapped_transactions.append(txn)

            if verbose and (i + 1) % 10 == 0:
                print(f"  Processed {i+1}/{len(stripe_customers)}...")

        except Exception as e:
            errors.append(f"Customer {sc.get('id', '?')}: {e}")

    # ── Step 3: Write to DB ────────────────────────────────────────────────────
    if verbose:
        print(f"\n[3/3] Writing to database...")

    write_to_db(mapped_customers, mapped_transactions, company_id)

    # ── Summary ────────────────────────────────────────────────────────────────
    active   = sum(1 for c in mapped_customers if not c["is_churned"])
    churned  = sum(1 for c in mapped_customers if c["is_churned"])
    total_mrr = sum(c["mrr"] for c in mapped_customers)

    summary = {
        "company_id":    company_id,
        "customers":     len(mapped_customers),
        "active":        active,
        "churned":       churned,
        "transactions":  len(mapped_transactions),
        "total_mrr":     round(total_mrr, 2),
        "errors":        errors,
        "synced_at":     datetime.now().isoformat(),
    }

    if verbose:
        print(f"\n── Sync Complete ────────────────────────────────────")
        print(f"  Customers synced  : {len(mapped_customers)}")
        print(f"  Active            : {active}")
        print(f"  Churned           : {churned}")
        print(f"  Transactions      : {len(mapped_transactions)}")
        print(f"  Total MRR         : ${total_mrr:,.2f}")
        if errors:
            print(f"  Errors            : {len(errors)}")
            for e in errors[:5]:
                print(f"    - {e}")
        print(f"────────────────────────────────────────────────────\n")

    return summary


# ── Status checker ─────────────────────────────────────────────────────────────

def show_status(company_id: str):
    """Show what Stripe data is currently in the EZER database."""
    from utils.db import get_connection

    conn = get_connection()

    customers = conn.execute(
        "SELECT COUNT(*) FROM customers WHERE company_id = ?", (company_id,)
    ).fetchone()[0]

    active = conn.execute(
        "SELECT COUNT(*) FROM customers WHERE company_id = ? AND is_churned = 0",
        (company_id,)
    ).fetchone()[0]

    mrr = conn.execute(
        "SELECT COALESCE(SUM(mrr), 0) FROM customers WHERE company_id = ? AND is_churned = 0",
        (company_id,)
    ).fetchone()[0]

    transactions = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE company_id = ?", (company_id,)
    ).fetchone()[0]

    conn.close()

    print(f"\n── EZER Database Status ─────────────────────────────")
    print(f"  Company ID   : {company_id}")
    print(f"  Customers    : {customers}")
    print(f"  Active       : {active}")
    print(f"  Total MRR    : ${mrr:,.2f}")
    print(f"  Transactions : {transactions}")
    print(f"────────────────────────────────────────────────────\n")


# ── API endpoints helper ───────────────────────────────────────────────────────

def add_stripe_endpoints(app, require_auth):
    """
    Add Stripe connector endpoints to the FastAPI app.
    Call this from api.py to expose Stripe sync via REST.

    Endpoints added:
      POST /connectors/stripe/sync    — trigger a full sync
      GET  /connectors/stripe/status  — show sync status
      POST /connectors/stripe/test    — test Stripe connection
    """
    from fastapi import Depends, HTTPException
    from pydantic import BaseModel

    class StripeConnectorSyncRequest(BaseModel):
        fetch_invoices: bool = True

    @app.post("/connectors/stripe/sync", tags=["Connectors"])
    def stripe_sync(
        req:     StripeConnectorSyncRequest,
        company: dict = Depends(require_auth),
    ):
        """
        Sync all Stripe customers and transactions into EZER.
        Requires STRIPE_SECRET_KEY environment variable to be set.
        """
        if not STRIPE_SECRET_KEY:
            raise HTTPException(
                status_code=400,
                detail="STRIPE_SECRET_KEY not configured. Set it in your environment variables."
            )
        summary = sync_stripe(
            company_id=company["company_id"],
            fetch_invoices=req.fetch_invoices,
            verbose=False,
        )
        return summary

    @app.get("/connectors/stripe/status", tags=["Connectors"])
    def stripe_status(company: dict = Depends(require_auth)):
        """Show current Stripe data in EZER database."""
        from utils.db import get_connection
        conn = get_connection()
        cid = company["company_id"]
        result = {
            "customers":    conn.execute("SELECT COUNT(*) FROM customers WHERE company_id=?", (cid,)).fetchone()[0],
            "active":       conn.execute("SELECT COUNT(*) FROM customers WHERE company_id=? AND is_churned=0", (cid,)).fetchone()[0],
            "transactions": conn.execute("SELECT COUNT(*) FROM transactions WHERE company_id=?", (cid,)).fetchone()[0],
            "total_mrr":    conn.execute("SELECT COALESCE(SUM(mrr),0) FROM customers WHERE company_id=? AND is_churned=0", (cid,)).fetchone()[0],
        }
        conn.close()
        return result

    @app.post("/connectors/stripe/test", tags=["Connectors"])
    def stripe_test(company: dict = Depends(require_auth)):
        """Test Stripe API connection."""
        if not STRIPE_SECRET_KEY:
            raise HTTPException(status_code=400, detail="STRIPE_SECRET_KEY not set.")
        try:
            stripe = get_stripe()
            account = stripe.Account.retrieve()
            return {
                "connected": True,
                "account_name": account.get("business_profile", {}).get("name"),
                "country": account.get("country"),
                "mode": "live" if "live" in STRIPE_SECRET_KEY else "test",
            }
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="EZER — Stripe Connector")
    parser.add_argument("--sync",       action="store_true", help="Sync Stripe data to EZER")
    parser.add_argument("--test",       action="store_true", help="Test Stripe connection")
    parser.add_argument("--status",     action="store_true", help="Show DB status")
    parser.add_argument("--company-id", default="default",   help="Company/tenant ID")
    parser.add_argument("--no-invoices",action="store_true", help="Skip invoice sync")
    args = parser.parse_args()

    if args.test:
        test_connection()

    elif args.sync:
        if not STRIPE_SECRET_KEY:
            print("❌ STRIPE_SECRET_KEY not set.")
            print("   Get your key: https://dashboard.stripe.com/apikeys")
            print("   Then: export STRIPE_SECRET_KEY=sk_live_...")
            sys.exit(1)
        sync_stripe(
            company_id=args.company_id,
            fetch_invoices=not args.no_invoices,
        )

    elif args.status:
        show_status(args.company_id)

    else:
        parser.print_help()
