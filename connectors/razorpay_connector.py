"""
EZER — Razorpay Connector
=========================
Pulls real customer and payment data from a Razorpay account
and maps it into EZER's database schema.

What it pulls:
  - Contacts / Customers
  - Subscriptions (plan, status, MRR)
  - Payments (transactions — captured, failed, refunded)

What it maps to:
  - customers table
  - transactions table

Setup:
  1. Go to dashboard.razorpay.com → Settings → API Keys
  2. Generate a Key ID and Key Secret
  3. Set environment variables:
       RAZORPAY_KEY_ID=rzp_live_xxxxx
       RAZORPAY_KEY_SECRET=your_secret
  4. Run: python connectors/razorpay_connector.py --sync

Usage:
  python connectors/razorpay_connector.py --test
  python connectors/razorpay_connector.py --sync --company-id YOUR_ID
  python connectors/razorpay_connector.py --status --company-id YOUR_ID
"""

from __future__ import annotations
import os
import uuid
import sys
import argparse
import requests
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Config ─────────────────────────────────────────────────────────────────────

RAZORPAY_BASE_URL = "https://api.razorpay.com/v1"


# ── Auth ───────────────────────────────────────────────────────────────────────

def _get_auth(key_id: str, key_secret: str) -> tuple:
    """Return Basic auth tuple for Razorpay API."""
    if not key_id or not key_secret:
        raise ValueError(
            "Razorpay credentials not provided.\n"
            "Pass key_id= and key_secret= to sync_razorpay(), or set "
            "RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET for CLI use.\n"
            "Get keys: https://dashboard.razorpay.com/app/keys"
        )
    return (key_id, key_secret)


def razorpay_get(endpoint: str, key_id: str, key_secret: str, params: dict = None) -> dict:
    """Make an authenticated GET request to Razorpay API."""
    url = f"{RAZORPAY_BASE_URL}/{endpoint}"
    response = requests.get(url, auth=_get_auth(key_id, key_secret), params=params or {})
    response.raise_for_status()
    return response.json()


def test_connection(key_id: str = None, key_secret: str = None) -> bool:
    """Test Razorpay API connection."""
    kid    = key_id    or os.environ.get("RAZORPAY_KEY_ID", "")
    ksecret = key_secret or os.environ.get("RAZORPAY_KEY_SECRET", "")
    try:
        razorpay_get("payments", kid, ksecret, {"count": 1})
        mode = "LIVE" if kid.startswith("rzp_live") else "TEST"
        print(f"\n✅ Razorpay connected")
        print(f"   Key ID : {kid[:12]}...")
        print(f"   Mode   : {mode}\n")
        return True
    except Exception as e:
        print(f"\n❌ Razorpay connection failed: {e}\n")
        return False


# ── Fetchers ───────────────────────────────────────────────────────────────────

def fetch_all(endpoint: str, key_id: str, key_secret: str, count: int = 100) -> list[dict]:
    """Fetch all records from a paginated Razorpay endpoint."""
    items = []
    skip  = 0

    while True:
        try:
            result = razorpay_get(endpoint, key_id, key_secret, {"count": count, "skip": skip})
            batch  = result.get("items", [])
            items.extend(batch)

            if len(batch) < count:
                break
            skip += count
        except Exception as e:
            print(f"  Warning: fetch error at skip={skip}: {e}")
            break

    return items


def fetch_payments(key_id: str, key_secret: str, count: int = 100) -> list[dict]:
    """Fetch all payments from Razorpay."""
    print("  Fetching payments from Razorpay...")
    payments = fetch_all("payments", key_id, key_secret, count)
    print(f"  Found {len(payments)} payments")
    return payments


# ── Mappers ────────────────────────────────────────────────────────────────────

def map_customers_from_payments(payments: list[dict], company_id: str) -> list[dict]:
    """Build unique customers from payment records."""
    seen_emails = {}
    customers   = []
    now         = datetime.now(timezone.utc).isoformat()

    for payment in payments:
        email = payment.get("email", "")
        if not email:
            continue

        if email not in seen_emails:
            created_ts  = payment.get("created_at", 0)
            signup_date = datetime.fromtimestamp(
                created_ts, tz=timezone.utc
            ).strftime("%Y-%m-%d") if created_ts else now[:10]

            customer = {
                "customer_id":  str(uuid.uuid4()),
                "razorpay_id":  payment.get("customer_id", ""),
                "name":         payment.get("contact") or email.split("@")[0],
                "email":        email[:200],
                "company":      email.split("@")[1].split(".")[0].title() if "@" in email else "Unknown",
                "industry":     "Unknown",
                "plan":         "Starter",
                "mrr":          0.0,
                "signup_date":  signup_date,
                "country":      "IN",
                "employee_count": 0,
                "health_score": 65.0,
                "nps_score":    None,
                "is_churned":   0,
                "churn_date":   None,
                "created_at":   now,
                "company_id":   company_id,
                "_payments":    [],
            }
            seen_emails[email] = customer
            customers.append(customer)

        seen_emails[email]["_payments"].append(payment)

    for customer in customers:
        payments_hist = customer.pop("_payments", [])
        captured = [p for p in payments_hist if p.get("status") == "captured"]
        failed   = [p for p in payments_hist if p.get("status") == "failed"]

        if captured:
            recent = sorted(captured, key=lambda x: x.get("created_at", 0), reverse=True)
            if recent:
                last_amount = recent[0].get("amount", 0) / 100
                customer["mrr"] = round(last_amount, 2)
                customer["health_score"] = 75.0

        if failed and captured:
            failure_rate = len(failed) / (len(captured) + len(failed))
            if failure_rate > 0.3:
                customer["health_score"] = max(30.0, customer["health_score"] - 25)
            elif failure_rate > 0.1:
                customer["health_score"] = max(50.0, customer["health_score"] - 10)

        if captured:
            last_payment_ts = max(p.get("created_at", 0) for p in captured)
            days_since = (datetime.now(timezone.utc).timestamp() - last_payment_ts) / 86400
            if days_since > 60:
                customer["is_churned"] = 1
                customer["health_score"] = 15.0
                last_dt = datetime.fromtimestamp(last_payment_ts, tz=timezone.utc)
                customer["churn_date"] = last_dt.strftime("%Y-%m-%d")

    return customers


def map_payment_to_transaction(
    payment: dict,
    customer_id: str,
    company_id: str,
) -> dict | None:
    """Map a Razorpay payment to EZER's transactions schema."""
    if not payment.get("id"):
        return None

    amount   = payment.get("amount", 0) / 100
    currency = payment.get("currency", "INR")

    if payment.get("subscription_id"):
        txn_type = "subscription_renewal"
    elif payment.get("description", "").lower().find("new") >= 0:
        txn_type = "subscription_new"
    else:
        txn_type = "payment"

    razorpay_status = payment.get("status", "")
    status_map = {
        "captured":   "completed",
        "authorized": "pending",
        "failed":     "failed",
        "refunded":   "refunded",
    }
    status = status_map.get(razorpay_status, "pending")

    ts = payment.get("created_at", 0)
    txn_date = datetime.fromtimestamp(
        ts, tz=timezone.utc
    ).strftime("%Y-%m-%d") if ts else datetime.now().strftime("%Y-%m-%d")

    return {
        "txn_id":       str(uuid.uuid4()),
        "razorpay_id":  payment.get("id", ""),
        "customer_id":  customer_id,
        "amount":       amount,
        "currency":     currency,
        "txn_type":     txn_type,
        "product_id":   payment.get("subscription_id", "") or payment.get("order_id", ""),
        "status":       status,
        "txn_date":     txn_date,
        "company_id":   company_id,
    }


# ── DB writer ──────────────────────────────────────────────────────────────────

def write_to_db(
    customers:    list[dict],
    transactions: list[dict],
    company_id:   str,
):
    """Write mapped Razorpay data to EZER's database."""
    from utils.db import get_connection, DB_ENGINE

    conn = get_connection()

    print("  Clearing existing data for fresh sync...")
    conn.execute("DELETE FROM transactions WHERE company_id = ?", (company_id,))
    conn.execute("DELETE FROM customers WHERE company_id = ?", (company_id,))
    conn.commit()

    print(f"  Writing {len(customers)} customers...")
    customer_cols = [
        "customer_id", "name", "email", "company", "industry",
        "plan", "mrr", "signup_date", "country", "employee_count",
        "health_score", "nps_score", "is_churned", "churn_date",
        "created_at", "company_id"
    ]

    if DB_ENGINE == "postgresql":
        ph  = ", ".join(["%s"] * len(customer_cols))
        sql = f"INSERT INTO customers ({', '.join(customer_cols)}) VALUES ({ph}) ON CONFLICT DO NOTHING"
        data = [tuple(c.get(col) for col in customer_cols) for c in customers]
    else:
        ph  = ", ".join([f":{c}" for c in customer_cols])
        sql = f"INSERT OR REPLACE INTO customers ({', '.join(customer_cols)}) VALUES ({ph})"
        data = customers

    conn.executemany(sql, data)
    conn.commit()

    print(f"  Writing {len(transactions)} transactions...")
    txn_cols = [
        "txn_id", "customer_id", "amount", "currency",
        "txn_type", "product_id", "status", "txn_date", "company_id"
    ]

    if transactions:
        if DB_ENGINE == "postgresql":
            ph  = ", ".join(["%s"] * len(txn_cols))
            sql = f"INSERT INTO transactions ({', '.join(txn_cols)}) VALUES ({ph}) ON CONFLICT DO NOTHING"
            data = [tuple(t.get(col) for col in txn_cols) for t in transactions]
        else:
            ph  = ", ".join([f":{c}" for c in txn_cols])
            sql = f"INSERT OR REPLACE INTO transactions ({', '.join(txn_cols)}) VALUES ({ph})"
            data = transactions

        conn.executemany(sql, data)
        conn.commit()

    conn.close()


# ── Main sync ──────────────────────────────────────────────────────────────────

def sync_razorpay(
    company_id:          str,
    key_id:              str = None,
    key_secret:          str = None,
    fetch_payments_flag: bool = True,
    verbose:             bool = True,
) -> dict:
    """
    Full Razorpay → EZER sync.

    Args:
        company_id:          EZER tenant ID
        key_id:              Razorpay Key ID for this tenant.
        key_secret:          Razorpay Key Secret for this tenant.
        fetch_payments_flag: Whether to fetch payment history
        verbose:             Print progress

    Returns:
        Summary dict
    """
    kid     = key_id     or os.environ.get("RAZORPAY_KEY_ID", "")
    ksecret = key_secret or os.environ.get("RAZORPAY_KEY_SECRET", "")

    if not kid or not ksecret:
        raise ValueError("Razorpay credentials not provided.")

    if verbose:
        print(f"\n── Razorpay Sync ────────────────────────────────────")
        print(f"  Company ID : {company_id}")
        print(f"  Mode       : {'LIVE' if 'live' in kid else 'TEST'}")
        print()

    errors = []

    if verbose:
        print("[1/3] Fetching payments from Razorpay...")

    payments = fetch_payments(kid, ksecret) if fetch_payments_flag else []

    if verbose:
        print(f"\n[2/3] Building customers from {len(payments)} payments...")

    customers    = map_customers_from_payments(payments, company_id)
    transactions = []

    email_to_id = {c["email"]: c["customer_id"] for c in customers}

    for payment in payments:
        try:
            email = payment.get("email", "")
            cid   = email_to_id.get(email)
            if cid:
                txn = map_payment_to_transaction(payment, cid, company_id)
                if txn:
                    transactions.append(txn)
        except Exception as e:
            errors.append(f"Payment {payment.get('id', '?')}: {e}")

    if verbose:
        print(f"\n[3/3] Writing to database...")

    write_to_db(customers, transactions, company_id)

    active    = sum(1 for c in customers if not c["is_churned"])
    churned   = sum(1 for c in customers if c["is_churned"])
    total_mrr = sum(c["mrr"] for c in customers)

    summary = {
        "company_id":   company_id,
        "customers":    len(customers),
        "active":       active,
        "churned":      churned,
        "transactions": len(transactions),
        "total_mrr":    round(total_mrr, 2),
        "errors":       errors,
        "synced_at":    datetime.now().isoformat(),
    }

    if verbose:
        print(f"\n── Sync Complete ────────────────────────────────────")
        print(f"  Customers synced  : {len(customers)}")
        print(f"  Active            : {active}")
        print(f"  Churned           : {churned}")
        print(f"  Transactions      : {len(transactions)}")
        print(f"  Total MRR (INR)   : ₹{total_mrr:,.2f}")
        if errors:
            print(f"  Errors            : {len(errors)}")
            for e in errors[:5]:
                print(f"    - {e}")
        print(f"────────────────────────────────────────────────────\n")

    return summary


# ── Status ─────────────────────────────────────────────────────────────────────

def show_status(company_id: str):
    """Show Razorpay data in EZER database."""
    from utils.db import get_connection
    conn = get_connection()

    customers    = conn.execute("SELECT COUNT(*) FROM customers WHERE company_id=?", (company_id,)).fetchone()[0]
    active       = conn.execute("SELECT COUNT(*) FROM customers WHERE company_id=? AND is_churned=0", (company_id,)).fetchone()[0]
    mrr          = conn.execute("SELECT COALESCE(SUM(mrr),0) FROM customers WHERE company_id=? AND is_churned=0", (company_id,)).fetchone()[0]
    transactions = conn.execute("SELECT COUNT(*) FROM transactions WHERE company_id=?", (company_id,)).fetchone()[0]
    conn.close()

    print(f"\n── EZER Database Status (Razorpay) ──────────────────")
    print(f"  Company ID   : {company_id}")
    print(f"  Customers    : {customers}")
    print(f"  Active       : {active}")
    print(f"  Total MRR    : ₹{mrr:,.2f}")
    print(f"  Transactions : {transactions}")
    print(f"────────────────────────────────────────────────────\n")


# ── API endpoints ──────────────────────────────────────────────────────────────

def add_razorpay_endpoints(app, require_auth):
    """Add Razorpay connector endpoints to FastAPI app."""
    from fastapi import Depends, HTTPException
    from pydantic import BaseModel

    class RazorpayConnectorSyncRequest(BaseModel):
        fetch_payments: bool = True

    @app.post("/connectors/razorpay/sync", tags=["Connectors"])
    def razorpay_sync(
        req:     RazorpayConnectorSyncRequest,
        company: dict = Depends(require_auth),
    ):
        """Sync all Razorpay payments and customers into EZER."""
        key_id     = company.get("razorpay_key_id") or ""
        key_secret = company.get("razorpay_key_secret") or ""
        if not key_id or not key_secret:
            raise HTTPException(
                status_code=400,
                detail="Razorpay credentials not configured for this account."
            )
        try:
            return sync_razorpay(
                company_id=company["company_id"],
                key_id=key_id,
                key_secret=key_secret,
                fetch_payments_flag=req.fetch_payments,
                verbose=False,
            )
        except Exception:
            raise HTTPException(status_code=500, detail="Razorpay sync failed")

    @app.get("/connectors/razorpay/status", tags=["Connectors"])
    def razorpay_status(company: dict = Depends(require_auth)):
        """Show current Razorpay data in EZER database."""
        from utils.db import get_connection
        conn = get_connection()
        cid  = company["company_id"]
        result = {
            "customers":    conn.execute("SELECT COUNT(*) FROM customers WHERE company_id=?", (cid,)).fetchone()[0],
            "active":       conn.execute("SELECT COUNT(*) FROM customers WHERE company_id=? AND is_churned=0", (cid,)).fetchone()[0],
            "transactions": conn.execute("SELECT COUNT(*) FROM transactions WHERE company_id=?", (cid,)).fetchone()[0],
            "total_mrr_inr": conn.execute("SELECT COALESCE(SUM(mrr),0) FROM customers WHERE company_id=? AND is_churned=0", (cid,)).fetchone()[0],
        }
        conn.close()
        return result

    @app.post("/connectors/razorpay/test", tags=["Connectors"])
    def razorpay_test(company: dict = Depends(require_auth)):
        """Test Razorpay API connection."""
        key_id     = company.get("razorpay_key_id") or ""
        key_secret = company.get("razorpay_key_secret") or ""
        if not key_id:
            raise HTTPException(status_code=400, detail="Razorpay credentials not configured.")
        success = test_connection(key_id, key_secret)
        if not success:
            raise HTTPException(status_code=400, detail="Razorpay connection test failed")
        return {
            "connected": True,
            "key_id": key_id[:12] + "...",
            "mode": "live" if "live" in key_id else "test",
        }


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="EZER — Razorpay Connector")
    parser.add_argument("--sync",        action="store_true", help="Sync Razorpay data to EZER")
    parser.add_argument("--test",        action="store_true", help="Test Razorpay connection")
    parser.add_argument("--status",      action="store_true", help="Show DB status")
    parser.add_argument("--company-id",  default="default",   help="Company/tenant ID")
    parser.add_argument("--no-payments", action="store_true", help="Skip payment fetch")
    args = parser.parse_args()

    _cli_key_id     = os.environ.get("RAZORPAY_KEY_ID", "")
    _cli_key_secret = os.environ.get("RAZORPAY_KEY_SECRET", "")

    if args.test:
        test_connection(_cli_key_id, _cli_key_secret)

    elif args.sync:
        if not _cli_key_id or not _cli_key_secret:
            print("❌ Razorpay credentials not set.")
            print("   export RAZORPAY_KEY_ID=rzp_live_xxxxx")
            print("   export RAZORPAY_KEY_SECRET=your_secret")
            sys.exit(1)
        sync_razorpay(
            company_id=args.company_id,
            key_id=_cli_key_id,
            key_secret=_cli_key_secret,
            fetch_payments_flag=not args.no_payments,
        )

    elif args.status:
        show_status(args.company_id)

    else:
        parser.print_help()
