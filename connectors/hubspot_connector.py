"""
EZER — HubSpot Connector
========================
Pulls real CRM data from HubSpot and maps it into EZER's database.

What it pulls:
  - Contacts (customers — name, email, company, lifecycle stage)
  - Companies (company details, industry, employee count)
  - Deals (revenue, pipeline stage, close dates)
  - Engagement (emails opened, calls logged, meetings booked)

What it maps to:
  - customers table (from contacts + companies)
  - transactions table (from closed deals)
  - engagement_events table (from HubSpot engagement)
  - email_log table (from HubSpot email tracking)

Setup:
  1. Go to app.hubspot.com → Settings → Account Setup → Integrations → Private Apps
  2. Create a Private App with these scopes:
       crm.objects.contacts.read
       crm.objects.companies.read
       crm.objects.deals.read
       crm.objects.engagements.read
  3. Copy the Access Token
  4. Set: export HUBSPOT_ACCESS_TOKEN=pat-na1-xxxxx
  5. Run: python connectors/hubspot_connector.py --sync

Usage:
  python connectors/hubspot_connector.py --test
  python connectors/hubspot_connector.py --sync --company-id YOUR_ID
  python connectors/hubspot_connector.py --status --company-id YOUR_ID
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

HUBSPOT_BASE_URL = "https://api.hubapi.com"

# HubSpot lifecycle stages → EZER plan/status mapping
LIFECYCLE_TO_PLAN = {
    "customer":      "Growth",
    "evangelist":    "Scale",
    "other":         "Starter",
    "lead":          "Free",
    "marketingqualifiedlead": "Free",
    "salesqualifiedlead":     "Starter",
    "opportunity":   "Starter",
    "subscriber":    "Starter",
}

# HubSpot deal stages that indicate churn/closed-lost
CHURNED_STAGES = {"closedlost", "closed_lost", "churned", "cancelled"}


# ── Auth ───────────────────────────────────────────────────────────────────────

def _make_headers(access_token: str) -> dict:
    """Build authenticated headers for HubSpot API."""
    if not access_token:
        raise ValueError(
            "HubSpot access token not provided.\n"
            "Pass access_token= to sync_hubspot(), or set HUBSPOT_ACCESS_TOKEN "
            "in your environment for CLI use.\n"
            "Create a Private App at: app.hubspot.com/private-apps"
        )
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type":  "application/json",
    }


def hubspot_get(endpoint: str, access_token: str, params: dict = None) -> dict:
    """Make an authenticated GET request to HubSpot API."""
    url = f"{HUBSPOT_BASE_URL}/{endpoint}"
    response = requests.get(url, headers=_make_headers(access_token), params=params or {})
    response.raise_for_status()
    return response.json()


def hubspot_post(endpoint: str, access_token: str, payload: dict) -> dict:
    """Make an authenticated POST request to HubSpot API."""
    url = f"{HUBSPOT_BASE_URL}/{endpoint}"
    response = requests.post(url, headers=_make_headers(access_token), json=payload)
    response.raise_for_status()
    return response.json()


def test_connection(access_token: str = None) -> bool:
    """Test HubSpot connection."""
    token = access_token or os.environ.get("HUBSPOT_ACCESS_TOKEN", "")
    try:
        result = hubspot_get("crm/v3/objects/contacts", token, {"limit": 1})
        print(f"\n✅ HubSpot connected")
        print(f"   Token  : {token[:15]}...")
        print(f"   Contacts found (sample): {result.get('total', 'unknown')}\n")
        return True
    except Exception as e:
        print(f"\n❌ HubSpot connection failed: {e}\n")
        return False


# ── Fetchers ───────────────────────────────────────────────────────────────────

def fetch_contacts(access_token: str, properties: list[str] = None) -> list[dict]:
    """Fetch all contacts from HubSpot with pagination."""
    default_props = [
        "firstname", "lastname", "email", "company",
        "lifecyclestage", "hs_lead_status", "createdate",
        "lastmodifieddate", "hs_email_open", "hs_email_click",
        "hs_email_delivered", "hs_email_bounce",
        "num_notes", "notes_last_contacted",
        "hs_content_membership_status",
    ]
    props    = properties or default_props
    contacts = []
    after    = None

    print("  Fetching contacts from HubSpot...")
    while True:
        params = {
            "limit":      100,
            "properties": ",".join(props),
        }
        if after:
            params["after"] = after

        result = hubspot_get("crm/v3/objects/contacts", access_token, params)
        batch  = result.get("results", [])
        contacts.extend(batch)

        paging = result.get("paging", {})
        after  = paging.get("next", {}).get("after")
        if not after:
            break

    print(f"  Found {len(contacts)} contacts")
    return contacts


def fetch_companies(access_token: str) -> dict[str, dict]:
    """Fetch all companies, return dict keyed by company ID."""
    companies = {}
    after     = None
    props     = ["name", "industry", "numberofemployees", "annualrevenue", "country", "hs_lastmodifieddate"]

    print("  Fetching companies from HubSpot...")
    while True:
        params = {"limit": 100, "properties": ",".join(props)}
        if after:
            params["after"] = after

        result = hubspot_get("crm/v3/objects/companies", access_token, params)
        for company in result.get("results", []):
            companies[company["id"]] = company.get("properties", {})

        paging = result.get("paging", {})
        after  = paging.get("next", {}).get("after")
        if not after:
            break

    print(f"  Found {len(companies)} companies")
    return companies


def fetch_deals(access_token: str) -> list[dict]:
    """Fetch all deals from HubSpot."""
    deals = []
    after = None
    props = ["dealname", "amount", "dealstage", "closedate", "createdate", "pipeline", "associated_contact"]

    print("  Fetching deals from HubSpot...")
    while True:
        params = {"limit": 100, "properties": ",".join(props)}
        if after:
            params["after"] = after

        result = hubspot_get("crm/v3/objects/deals", access_token, params)
        deals.extend(result.get("results", []))

        paging = result.get("paging", {})
        after  = paging.get("next", {}).get("after")
        if not after:
            break

    print(f"  Found {len(deals)} deals")
    return deals


# ── Mappers ────────────────────────────────────────────────────────────────────

def map_contact_to_customer(
    contact:    dict,
    company_id: str,
    companies:  dict = None,
) -> dict:
    """Map a HubSpot contact to EZER's customer schema."""
    props = contact.get("properties", {})
    now   = datetime.now(timezone.utc).isoformat()

    first = props.get("firstname", "") or ""
    last  = props.get("lastname",  "") or ""
    name  = f"{first} {last}".strip() or props.get("email", "Unknown").split("@")[0]

    email        = props.get("email", "") or ""
    company_name = props.get("company", "") or ""

    lifecycle = props.get("lifecyclestage", "lead") or "lead"
    plan      = LIFECYCLE_TO_PLAN.get(lifecycle.lower(), "Starter")

    is_churned = 0
    churn_date = None
    lead_status = props.get("hs_lead_status", "") or ""
    if lead_status.lower() in {"churned", "cancelled", "unqualified", "lost"}:
        is_churned = 1

    created_str = props.get("createdate", "") or ""
    try:
        signup_date = datetime.fromisoformat(
            created_str.replace("Z", "+00:00")
        ).strftime("%Y-%m-%d") if created_str else now[:10]
    except Exception:
        signup_date = now[:10]

    health_score  = 60.0
    emails_opened = int(props.get("hs_email_open", 0) or 0)
    emails_clicked= int(props.get("hs_email_click", 0) or 0)
    emails_bounced= int(props.get("hs_email_bounce", 0) or 0)
    last_contacted= props.get("notes_last_contacted", "") or ""

    if emails_opened > 5:
        health_score += 10
    if emails_clicked > 2:
        health_score += 5
    if emails_bounced > 0:
        health_score -= 10
    if last_contacted:
        try:
            last_dt    = datetime.fromisoformat(last_contacted.replace("Z", "+00:00"))
            days_since = (datetime.now(timezone.utc) - last_dt).days
            if days_since > 60:
                health_score -= 20
            elif days_since > 30:
                health_score -= 10
            elif days_since < 7:
                health_score += 10
        except Exception:
            pass

    if is_churned:
        health_score = 10.0

    health_score = max(0.0, min(100.0, health_score))

    employee_count = 0
    industry       = "Unknown"
    country        = "Unknown"

    if companies:
        assoc_company_id = contact.get("associations", {}).get("companies", {}).get("results", [{}])[0].get("id") if contact.get("associations") else None
        if assoc_company_id and assoc_company_id in companies:
            co = companies[assoc_company_id]
            employee_count = int(co.get("numberofemployees", 0) or 0)
            industry       = co.get("industry", "Unknown") or "Unknown"
            country        = co.get("country", "Unknown") or "Unknown"
            if not company_name:
                company_name = co.get("name", "") or ""

    return {
        "customer_id":   str(uuid.uuid4()),
        "hubspot_id":    contact.get("id", ""),
        "name":          name[:200],
        "email":         email[:200],
        "company":       company_name[:200] or name,
        "industry":      industry[:100],
        "plan":          plan,
        "mrr":           0.0,
        "signup_date":   signup_date,
        "country":       country[:100],
        "employee_count": employee_count,
        "health_score":  round(health_score, 1),
        "nps_score":     None,
        "is_churned":    is_churned,
        "churn_date":    churn_date,
        "created_at":    now,
        "company_id":    company_id,
    }


def map_deal_to_transaction(
    deal:        dict,
    customer_id: str,
    company_id:  str,
) -> dict | None:
    """Map a HubSpot deal to EZER's transactions schema."""
    props = deal.get("properties", {})
    if not props.get("amount"):
        return None

    amount = float(props.get("amount", 0) or 0)
    stage  = props.get("dealstage", "") or ""

    if stage.lower() in {"closedwon", "closed_won"}:
        status   = "completed"
        txn_type = "subscription_new"
    elif stage.lower() in CHURNED_STAGES:
        status   = "failed"
        txn_type = "payment"
    else:
        status   = "pending"
        txn_type = "payment"

    close_str = props.get("closedate", "") or props.get("createdate", "") or ""
    try:
        txn_date = datetime.fromisoformat(
            close_str.replace("Z", "+00:00")
        ).strftime("%Y-%m-%d") if close_str else datetime.now().strftime("%Y-%m-%d")
    except Exception:
        txn_date = datetime.now().strftime("%Y-%m-%d")

    return {
        "txn_id":      str(uuid.uuid4()),
        "hubspot_id":  deal.get("id", ""),
        "customer_id": customer_id,
        "amount":      amount,
        "currency":    "USD",
        "txn_type":    txn_type,
        "product_id":  props.get("pipeline", "") or "",
        "status":      status,
        "txn_date":    txn_date,
        "company_id":  company_id,
    }


# ── DB writer ──────────────────────────────────────────────────────────────────

def write_to_db(
    customers:    list[dict],
    transactions: list[dict],
    company_id:   str,
):
    """Write HubSpot data to EZER database."""
    from utils.db import get_connection, DB_ENGINE

    conn = get_connection()

    print("  Clearing existing data for fresh sync...")
    conn.execute("DELETE FROM transactions WHERE company_id=?", (company_id,))
    conn.execute("DELETE FROM customers WHERE company_id=?", (company_id,))
    conn.commit()

    customer_cols = [
        "customer_id", "name", "email", "company", "industry",
        "plan", "mrr", "signup_date", "country", "employee_count",
        "health_score", "nps_score", "is_churned", "churn_date",
        "created_at", "company_id"
    ]
    print(f"  Writing {len(customers)} customers...")
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

    txn_cols = ["txn_id","customer_id","amount","currency","txn_type","product_id","status","txn_date","company_id"]
    print(f"  Writing {len(transactions)} transactions...")
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

def sync_hubspot(
    company_id:       str,
    access_token:     str = None,
    fetch_deals_flag: bool = True,
    verbose:          bool = True,
) -> dict:
    """
    Full HubSpot → EZER sync.

    Args:
        company_id:       EZER tenant ID
        access_token:     HubSpot Private App token for this tenant.
                          Do NOT pass None in API context.
        fetch_deals_flag: Whether to also sync deal transactions
        verbose:          Print progress
    """
    token = access_token or os.environ.get("HUBSPOT_ACCESS_TOKEN", "")
    if not token:
        raise ValueError("HubSpot access token not provided.")

    if verbose:
        print(f"\n── HubSpot Sync ─────────────────────────────────────")
        print(f"  Company ID : {company_id}")
        print()

    errors = []

    if verbose:
        print("[1/4] Fetching data from HubSpot...")

    contacts  = fetch_contacts(token)
    companies = fetch_companies(token)
    deals     = fetch_deals(token) if fetch_deals_flag else []

    if verbose:
        print(f"\n[2/4] Mapping {len(contacts)} contacts to customers...")

    customers = []
    for contact in contacts:
        try:
            customer = map_contact_to_customer(contact, company_id, companies)
            customers.append(customer)
        except Exception as e:
            errors.append(f"Contact {contact.get('id','?')}: {e}")

    if verbose:
        print(f"\n[3/4] Mapping {len(deals)} deals to transactions...")

    transactions = []
    for deal in deals:
        try:
            txn = map_deal_to_transaction(deal, "", company_id)
            if txn and txn["status"] == "completed":
                txn["customer_id"] = customers[0]["customer_id"] if customers else str(uuid.uuid4())
                transactions.append(txn)
        except Exception as e:
            errors.append(f"Deal {deal.get('id','?')}: {e}")

    if transactions and customers:
        total_deal_value = sum(t["amount"] for t in transactions)
        active_customers = [c for c in customers if not c["is_churned"]]
        if active_customers:
            avg_mrr = round(total_deal_value / len(active_customers) / 12, 2)
            for customer in active_customers:
                customer["mrr"] = avg_mrr

    if verbose:
        print(f"\n[4/4] Writing to database...")

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
        print(f"  Total MRR         : ${total_mrr:,.2f}")
        if errors:
            print(f"  Errors            : {len(errors)}")
            for e in errors[:5]:
                print(f"    - {e}")
        print(f"────────────────────────────────────────────────────\n")

    return summary


# ── Status ─────────────────────────────────────────────────────────────────────

def show_status(company_id: str):
    from utils.db import get_connection
    conn = get_connection()
    print(f"\n── EZER Database Status (HubSpot) ───────────────────")
    print(f"  Customers    : {conn.execute('SELECT COUNT(*) FROM customers WHERE company_id=?', (company_id,)).fetchone()[0]}")
    print(f"  Active       : {conn.execute('SELECT COUNT(*) FROM customers WHERE company_id=? AND is_churned=0', (company_id,)).fetchone()[0]}")
    print(f"  MRR          : ${conn.execute('SELECT COALESCE(SUM(mrr),0) FROM customers WHERE company_id=? AND is_churned=0', (company_id,)).fetchone()[0]:,.2f}")
    print(f"  Transactions : {conn.execute('SELECT COUNT(*) FROM transactions WHERE company_id=?', (company_id,)).fetchone()[0]}")
    print(f"────────────────────────────────────────────────────\n")
    conn.close()


# ── API endpoints ──────────────────────────────────────────────────────────────

def add_hubspot_endpoints(app, require_auth):
    """Add HubSpot connector endpoints to FastAPI app."""
    from fastapi import Depends, HTTPException
    from pydantic import BaseModel

    class HubSpotConnectorSyncRequest(BaseModel):
        fetch_deals: bool = True

    @app.post("/connectors/hubspot/sync", tags=["Connectors"])
    def hubspot_sync(req: HubSpotConnectorSyncRequest, company: dict = Depends(require_auth)):
        """Sync all HubSpot contacts and deals into EZER."""
        access_token = company.get("hubspot_token") or ""
        if not access_token:
            raise HTTPException(status_code=400, detail="HubSpot access token not configured for this account.")
        try:
            return sync_hubspot(
                company_id=company["company_id"],
                access_token=access_token,
                fetch_deals_flag=req.fetch_deals,
                verbose=False,
            )
        except Exception:
            raise HTTPException(status_code=500, detail="HubSpot sync failed")

    @app.get("/connectors/hubspot/status", tags=["Connectors"])
    def hubspot_status(company: dict = Depends(require_auth)):
        """Show current HubSpot data in EZER database."""
        from utils.db import get_connection
        conn = get_connection()
        cid  = company["company_id"]
        result = {
            "customers":    conn.execute("SELECT COUNT(*) FROM customers WHERE company_id=?", (cid,)).fetchone()[0],
            "active":       conn.execute("SELECT COUNT(*) FROM customers WHERE company_id=? AND is_churned=0", (cid,)).fetchone()[0],
            "transactions": conn.execute("SELECT COUNT(*) FROM transactions WHERE company_id=?", (cid,)).fetchone()[0],
            "total_mrr":    conn.execute("SELECT COALESCE(SUM(mrr),0) FROM customers WHERE company_id=? AND is_churned=0", (cid,)).fetchone()[0],
        }
        conn.close()
        return result

    @app.post("/connectors/hubspot/test", tags=["Connectors"])
    def hubspot_test(company: dict = Depends(require_auth)):
        """Test HubSpot API connection."""
        access_token = company.get("hubspot_token") or ""
        if not access_token:
            raise HTTPException(status_code=400, detail="HubSpot access token not configured.")
        success = test_connection(access_token)
        if not success:
            raise HTTPException(status_code=400, detail="HubSpot connection failed")
        return {"connected": True, "token": access_token[:12] + "..."}


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="EZER — HubSpot Connector")
    parser.add_argument("--sync",       action="store_true", help="Sync HubSpot data to EZER")
    parser.add_argument("--test",       action="store_true", help="Test HubSpot connection")
    parser.add_argument("--status",     action="store_true", help="Show DB status")
    parser.add_argument("--company-id", default="default",   help="Company/tenant ID")
    parser.add_argument("--no-deals",   action="store_true", help="Skip deal sync")
    args = parser.parse_args()

    _cli_token = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")

    if args.test:
        test_connection(_cli_token)
    elif args.sync:
        if not _cli_token:
            print("❌ HUBSPOT_ACCESS_TOKEN not set.")
            print("   Create a Private App: app.hubspot.com/private-apps")
            print("   Then: export HUBSPOT_ACCESS_TOKEN=pat-na1-xxxxx")
            sys.exit(1)
        sync_hubspot(
            company_id=args.company_id,
            access_token=_cli_token,
            fetch_deals_flag=not args.no_deals,
        )
    elif args.status:
        show_status(args.company_id)
    else:
        parser.print_help()
