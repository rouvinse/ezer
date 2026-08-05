"""
EZER — Salesforce Connector
===========================
Pulls CRM data from Salesforce and maps it into EZER's database schema.

Salesforce is the dominant CRM for companies with $10M+ ARR.
It contains accounts, contacts, opportunities, and activity history.

What it pulls:
  - Accounts (companies — name, industry, employee count, ARR)
  - Contacts (customer contacts at each account)
  - Opportunities (deals — value, stage, close date)
  - Activities (tasks, calls, emails logged)

What it maps to:
  - customers table (from Accounts)
  - transactions table (from closed Opportunities)
  - engagement_events table (from Activities)

Authentication:
  Salesforce uses OAuth 2.0. Two options:
  Option A — Username/Password (simpler, for internal use):
    SALESFORCE_USERNAME=you@company.com
    SALESFORCE_PASSWORD=yourpassword
    SALESFORCE_SECURITY_TOKEN=yourtoken
    SALESFORCE_DOMAIN=login  (or your custom domain)

  Option B — Connected App OAuth (recommended for production):
    SALESFORCE_CLIENT_ID=your_consumer_key
    SALESFORCE_CLIENT_SECRET=your_consumer_secret
    SALESFORCE_USERNAME=you@company.com
    SALESFORCE_PASSWORD=yourpassword+securitytoken

Setup (Option A — quickest):
  1. Log into Salesforce → top right avatar → Settings
  2. Search "Reset My Security Token" → click Reset
  3. Check your email for the token
  4. Set environment variables and run sync

Usage:
  python connectors/salesforce_connector.py --test
  python connectors/salesforce_connector.py --sync --company-id YOUR_ID
  python connectors/salesforce_connector.py --status --company-id YOUR_ID
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

SF_USERNAME       = os.environ.get("SALESFORCE_USERNAME", "")
SF_PASSWORD       = os.environ.get("SALESFORCE_PASSWORD", "")
SF_SECURITY_TOKEN = os.environ.get("SALESFORCE_SECURITY_TOKEN", "")
SF_CLIENT_ID      = os.environ.get("SALESFORCE_CLIENT_ID", "")
SF_CLIENT_SECRET  = os.environ.get("SALESFORCE_CLIENT_SECRET", "")
SF_DOMAIN         = os.environ.get("SALESFORCE_DOMAIN", "login")

# Auth state (set after login)
_sf_access_token  = None
_sf_instance_url  = None


# ── Authentication ─────────────────────────────────────────────────────────────

def authenticate() -> tuple[str, str]:
    """
    Authenticate with Salesforce and return (access_token, instance_url).
    Supports both Username/Password and Connected App OAuth flows.
    """
    global _sf_access_token, _sf_instance_url

    if _sf_access_token and _sf_instance_url:
        return _sf_access_token, _sf_instance_url

    if not SF_USERNAME or not SF_PASSWORD:
        raise ValueError(
            "Salesforce credentials not set.\n"
            "Set these environment variables:\n"
            "  SALESFORCE_USERNAME=you@company.com\n"
            "  SALESFORCE_PASSWORD=yourpassword\n"
            "  SALESFORCE_SECURITY_TOKEN=yourtoken\n"
            "Get your security token: Salesforce → Settings → Reset My Security Token"
        )

    # Build auth payload
    if SF_CLIENT_ID and SF_CLIENT_SECRET:
        # Connected App OAuth
        payload = {
            "grant_type":    "password",
            "client_id":     SF_CLIENT_ID,
            "client_secret": SF_CLIENT_SECRET,
            "username":      SF_USERNAME,
            "password":      SF_PASSWORD + SF_SECURITY_TOKEN,
        }
    else:
        # Username/Password flow
        payload = {
            "grant_type": "password",
            "client_id":  "3MVG9pe2TCkV1DedT5uGI10K1J9XrYXX9EBiD9R5n1_",  # Default Salesforce client
            "client_secret": "",
            "username":   SF_USERNAME,
            "password":   SF_PASSWORD + SF_SECURITY_TOKEN,
        }

    login_url = f"https://{SF_DOMAIN}.salesforce.com/services/oauth2/token"
    response  = requests.post(login_url, data=payload)

    if response.status_code != 200:
        raise ValueError(
            f"Salesforce authentication failed: {response.json().get('error_description', response.text)}\n"
            "Check your username, password, and security token."
        )

    data               = response.json()
    _sf_access_token   = data["access_token"]
    _sf_instance_url   = data["instance_url"]

    return _sf_access_token, _sf_instance_url


def sf_get_headers() -> dict:
    """Get authenticated headers for Salesforce API."""
    token, _ = authenticate()
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
    }


def sf_query(soql: str) -> list[dict]:
    """
    Execute a SOQL query and return all records with pagination.
    SOQL is Salesforce's SQL-like query language.
    """
    _, instance_url = authenticate()
    url     = f"{instance_url}/services/data/v58.0/query"
    records = []
    params  = {"q": soql}

    while True:
        response = requests.get(url, headers=sf_get_headers(), params=params)
        response.raise_for_status()
        data = response.json()

        records.extend(data.get("records", []))

        if data.get("done", True):
            break

        # Next page
        next_url = data.get("nextRecordsUrl", "")
        if not next_url:
            break

        _, instance_url = authenticate()
        url    = f"{instance_url}{next_url}"
        params = {}

    return records


def test_connection() -> bool:
    """Test Salesforce connection."""
    try:
        token, instance_url = authenticate()
        # Quick test query
        records = sf_query("SELECT Id, Name FROM Account LIMIT 1")
        print(f"\n✅ Salesforce connected")
        print(f"   Instance : {instance_url}")
        print(f"   User     : {SF_USERNAME}")
        print(f"   Test     : Found {len(records)} account(s)\n")
        return True
    except Exception as e:
        print(f"\n❌ Salesforce connection failed: {e}\n")
        return False


# ── Fetchers ───────────────────────────────────────────────────────────────────

def fetch_accounts() -> list[dict]:
    """Fetch all Account records from Salesforce."""
    print("  Fetching accounts from Salesforce...")
    soql = """
        SELECT Id, Name, Industry, AnnualRevenue, NumberOfEmployees,
               BillingCountry, Type, Website, Description,
               CreatedDate, LastActivityDate, LastModifiedDate,
               OwnerId, AccountSource
        FROM Account
        WHERE IsDeleted = false
        ORDER BY LastModifiedDate DESC
    """
    records = sf_query(soql)
    print(f"  Found {len(records)} accounts")
    return records


def fetch_contacts() -> list[dict]:
    """Fetch all Contact records from Salesforce."""
    print("  Fetching contacts from Salesforce...")
    soql = """
        SELECT Id, AccountId, FirstName, LastName, Email, Title,
               Phone, CreatedDate, LastActivityDate,
               HasOptedOutOfEmail, DoNotCall
        FROM Contact
        WHERE IsDeleted = false
        ORDER BY CreatedDate DESC
    """
    records = sf_query(soql)
    print(f"  Found {len(records)} contacts")
    return records


def fetch_opportunities() -> list[dict]:
    """Fetch all Opportunity records from Salesforce."""
    print("  Fetching opportunities from Salesforce...")
    soql = """
        SELECT Id, AccountId, Name, Amount, StageName, CloseDate,
               CreatedDate, Probability, Type, LeadSource,
               IsClosed, IsWon
        FROM Opportunity
        WHERE IsDeleted = false
        ORDER BY CloseDate DESC
    """
    records = sf_query(soql)
    print(f"  Found {len(records)} opportunities")
    return records


def fetch_activities(account_ids: list[str]) -> list[dict]:
    """Fetch recent activities (tasks and events) for accounts."""
    if not account_ids:
        return []

    print("  Fetching activities from Salesforce...")
    # Batch by 200 IDs max per query
    all_activities = []
    for i in range(0, len(account_ids), 200):
        batch = account_ids[i:i+200]
        ids_str = "', '".join(batch)
        soql = f"""
            SELECT Id, WhatId, Subject, Status, ActivityDate,
                   Description, Type, CreatedDate
            FROM Task
            WHERE WhatId IN ('{ids_str}')
            AND IsDeleted = false
            ORDER BY ActivityDate DESC
            LIMIT 1000
        """
        try:
            records = sf_query(soql)
            all_activities.extend(records)
        except Exception as e:
            print(f"  Warning: activity fetch error: {e}")

    print(f"  Found {len(all_activities)} activities")
    return all_activities


# ── Mappers ────────────────────────────────────────────────────────────────────

def parse_sf_date(date_str: str) -> str:
    """Parse Salesforce date/datetime string to YYYY-MM-DD."""
    if not date_str:
        return datetime.now().strftime("%Y-%m-%d")
    try:
        if "T" in date_str:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        else:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


def get_plan_from_revenue(annual_revenue: float) -> str:
    """Derive a plan tier from annual revenue."""
    if not annual_revenue:
        return "Starter"
    if annual_revenue < 50000:
        return "Starter"
    elif annual_revenue < 200000:
        return "Growth"
    else:
        return "Scale"


def calculate_health_score(account: dict, opportunities: list[dict]) -> float:
    """Calculate customer health score from Salesforce signals."""
    health = 60.0

    # Last activity recency
    last_activity = account.get("LastActivityDate")
    if last_activity:
        try:
            last_dt    = datetime.strptime(last_activity, "%Y-%m-%d")
            days_since = (datetime.now() - last_dt).days
            if days_since < 7:
                health += 20
            elif days_since < 30:
                health += 10
            elif days_since < 60:
                health -= 5
            elif days_since < 90:
                health -= 15
            else:
                health -= 25
        except Exception:
            pass

    # Won opportunities = healthy signal
    won = [o for o in opportunities if o.get("IsWon")]
    lost = [o for o in opportunities if o.get("IsClosed") and not o.get("IsWon")]

    if won:
        health += min(15, len(won) * 5)
    if lost:
        health -= min(20, len(lost) * 8)

    return round(max(0.0, min(100.0, health)), 1)


def map_account_to_customer(
    account:      dict,
    company_id:   str,
    opps_by_acct: dict,
) -> dict:
    """Map a Salesforce Account to EZER's customer schema."""
    now = datetime.now(timezone.utc).isoformat()

    account_id    = account.get("Id", "")
    name          = account.get("Name", "Unknown") or "Unknown"
    industry      = account.get("Industry", "Unknown") or "Unknown"
    annual_rev    = float(account.get("AnnualRevenue", 0) or 0)
    mrr           = round(annual_rev / 12, 2)
    employees     = int(account.get("NumberOfEmployees", 0) or 0)
    country       = account.get("BillingCountry", "Unknown") or "Unknown"
    plan          = get_plan_from_revenue(annual_rev)
    signup_date   = parse_sf_date(account.get("CreatedDate", ""))

    # Get opportunities for this account
    acct_opps = opps_by_acct.get(account_id, [])
    health    = calculate_health_score(account, acct_opps)

    # Churn detection — closed lost opportunities or no activity in 180 days
    is_churned = 0
    churn_date = None
    last_activity = account.get("LastActivityDate")

    if last_activity:
        try:
            last_dt    = datetime.strptime(last_activity, "%Y-%m-%d")
            days_since = (datetime.now() - last_dt).days
            if days_since > 180:
                is_churned = 1
                health     = 10.0
                churn_date = last_activity
        except Exception:
            pass

    return {
        "customer_id":    str(uuid.uuid4()),
        "salesforce_id":  account_id,
        "name":           name[:200],
        "email":          f"account_{account_id[:8]}@salesforce.placeholder",
        "company":        name[:200],
        "industry":       industry[:100],
        "plan":           plan,
        "mrr":            mrr,
        "signup_date":    signup_date,
        "country":        country[:100],
        "employee_count": employees,
        "health_score":   health,
        "nps_score":      None,
        "is_churned":     is_churned,
        "churn_date":     churn_date,
        "created_at":     now,
        "company_id":     company_id,
    }


def map_opportunity_to_transaction(
    opp:         dict,
    customer_id: str,
    company_id:  str,
) -> dict | None:
    """Map a Salesforce Opportunity to EZER's transactions schema."""
    if not opp.get("Id") or not opp.get("Amount"):
        return None

    amount = float(opp.get("Amount", 0) or 0)
    stage  = opp.get("StageName", "") or ""
    is_won = opp.get("IsWon", False)

    if is_won:
        status   = "completed"
        txn_type = "subscription_new"
    elif opp.get("IsClosed") and not is_won:
        status   = "failed"
        txn_type = "payment"
    else:
        status   = "pending"
        txn_type = "payment"

    close_date = parse_sf_date(opp.get("CloseDate", ""))

    return {
        "txn_id":         str(uuid.uuid4()),
        "salesforce_id":  opp.get("Id", ""),
        "customer_id":    customer_id,
        "amount":         amount,
        "currency":       "USD",
        "txn_type":       txn_type,
        "product_id":     opp.get("Type", "") or "",
        "status":         status,
        "txn_date":       close_date,
        "company_id":     company_id,
    }


def map_activity_to_engagement(
    activity:    dict,
    customer_id: str,
    company_id:  str,
) -> dict | None:
    """Map a Salesforce Task to EZER's engagement_events schema."""
    subject = activity.get("Subject", "") or ""
    atype   = activity.get("Type", "") or ""

    type_map = {
        "Call":     "call",
        "Email":    "email_open",
        "Meeting":  "meeting",
        "Task":     "task",
        "Other":    "other",
    }
    event_type = type_map.get(atype, "other")

    activity_date = parse_sf_date(
        activity.get("ActivityDate") or activity.get("CreatedDate", "")
    )

    return {
        "event_id":        str(uuid.uuid4()),
        "customer_id":     customer_id,
        "event_type":      event_type,
        "channel":         "salesforce",
        "session_duration": 0,
        "event_date":      activity_date,
        "metadata":        subject[:100],
        "company_id":      company_id,
    }


# ── DB writer ──────────────────────────────────────────────────────────────────

def write_to_db(
    customers:    list[dict],
    transactions: list[dict],
    events:       list[dict],
    company_id:   str,
):
    """Write Salesforce data to EZER database."""
    from utils.db import get_connection, DB_ENGINE

    conn = get_connection()

    print("  Clearing existing data for fresh sync...")
    conn.execute("DELETE FROM engagement_events WHERE company_id=?", (company_id,))
    conn.execute("DELETE FROM transactions WHERE company_id=?", (company_id,))
    conn.execute("DELETE FROM customers WHERE company_id=?", (company_id,))
    conn.commit()

    def insert_batch(table, cols, rows):
        if not rows:
            return
        if DB_ENGINE == "postgresql":
            ph   = ", ".join(["%s"] * len(cols))
            sql  = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({ph}) ON CONFLICT DO NOTHING"
            data = [tuple(r.get(c) for c in cols) for r in rows]
        else:
            ph   = ", ".join([f":{c}" for c in cols])
            sql  = f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) VALUES ({ph})"
            data = rows
        conn.executemany(sql, data)
        conn.commit()

    customer_cols = [
        "customer_id", "name", "email", "company", "industry",
        "plan", "mrr", "signup_date", "country", "employee_count",
        "health_score", "nps_score", "is_churned", "churn_date",
        "created_at", "company_id"
    ]
    txn_cols = [
        "txn_id", "customer_id", "amount", "currency",
        "txn_type", "product_id", "status", "txn_date", "company_id"
    ]
    event_cols = [
        "event_id", "customer_id", "event_type", "channel",
        "session_duration", "event_date", "metadata", "company_id"
    ]

    print(f"  Writing {len(customers)} customers...")
    insert_batch("customers", customer_cols, customers)

    print(f"  Writing {len(transactions)} transactions...")
    insert_batch("transactions", txn_cols, transactions)

    print(f"  Writing {len(events)} engagement events...")
    insert_batch("engagement_events", event_cols, events)

    conn.close()


# ── Main sync ──────────────────────────────────────────────────────────────────

def sync_salesforce(
    company_id:          str,
    fetch_activities_flag: bool = True,
    verbose:             bool = True,
) -> dict:
    """Full Salesforce → EZER sync."""
    if verbose:
        print(f"\n── Salesforce Sync ──────────────────────────────────")
        print(f"  Company ID : {company_id}")
        print(f"  User       : {SF_USERNAME}")
        print()

    errors = []

    # ── Step 1: Fetch ──────────────────────────────────────────────────────────
    if verbose:
        print("[1/4] Fetching data from Salesforce...")

    accounts      = fetch_accounts()
    opportunities = fetch_opportunities()

    activities = []
    if fetch_activities_flag:
        account_ids = [a.get("Id") for a in accounts if a.get("Id")]
        activities  = fetch_activities(account_ids)

    # ── Step 2: Build lookup maps ──────────────────────────────────────────────
    if verbose:
        print(f"\n[2/4] Mapping {len(accounts)} accounts to customers...")

    # Group opportunities by account ID
    opps_by_acct = {}
    for opp in opportunities:
        acct_id = opp.get("AccountId", "")
        if acct_id:
            opps_by_acct.setdefault(acct_id, []).append(opp)

    # ── Step 3: Map accounts to customers ──────────────────────────────────────
    customers = []
    sf_to_customer_id = {}

    for account in accounts:
        try:
            customer = map_account_to_customer(account, company_id, opps_by_acct)
            customers.append(customer)
            sf_to_customer_id[account.get("Id", "")] = customer["customer_id"]
        except Exception as e:
            errors.append(f"Account {account.get('Id','?')}: {e}")

    # ── Step 4: Map opportunities and activities ────────────────────────────────
    if verbose:
        print(f"\n[3/4] Mapping {len(opportunities)} opportunities and {len(activities)} activities...")

    transactions = []
    for opp in opportunities:
        try:
            acct_id     = opp.get("AccountId", "")
            customer_id = sf_to_customer_id.get(acct_id)
            if customer_id:
                txn = map_opportunity_to_transaction(opp, customer_id, company_id)
                if txn:
                    transactions.append(txn)
        except Exception as e:
            errors.append(f"Opportunity {opp.get('Id','?')}: {e}")

    events = []
    for activity in activities:
        try:
            what_id     = activity.get("WhatId", "")
            customer_id = sf_to_customer_id.get(what_id)
            if customer_id:
                event = map_activity_to_engagement(activity, customer_id, company_id)
                if event:
                    events.append(event)
        except Exception as e:
            errors.append(f"Activity {activity.get('Id','?')}: {e}")

    # ── Write to DB ────────────────────────────────────────────────────────────
    if verbose:
        print(f"\n[4/4] Writing to database...")

    write_to_db(customers, transactions, events, company_id)

    # ── Summary ────────────────────────────────────────────────────────────────
    active    = sum(1 for c in customers if not c["is_churned"])
    churned   = sum(1 for c in customers if c["is_churned"])
    total_mrr = sum(c["mrr"] for c in customers)

    summary = {
        "company_id":   company_id,
        "customers":    len(customers),
        "active":       active,
        "churned":      churned,
        "transactions": len(transactions),
        "events":       len(events),
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
        print(f"  Engagement events : {len(events)}")
        print(f"  Total MRR         : ${total_mrr:,.2f}")
        if errors:
            print(f"  Errors            : {len(errors)}")
            for e in errors[:3]:
                print(f"    - {e}")
        print(f"────────────────────────────────────────────────────\n")

    return summary


# ── Status ─────────────────────────────────────────────────────────────────────

def show_status(company_id: str):
    from utils.db import get_connection
    conn = get_connection()
    print(f"\n── EZER Database Status (Salesforce) ────────────────")
    print(f"  Customers    : {conn.execute('SELECT COUNT(*) FROM customers WHERE company_id=?', (company_id,)).fetchone()[0]}")
    print(f"  Active       : {conn.execute('SELECT COUNT(*) FROM customers WHERE company_id=? AND is_churned=0', (company_id,)).fetchone()[0]}")
    print(f"  MRR          : ${conn.execute('SELECT COALESCE(SUM(mrr),0) FROM customers WHERE company_id=? AND is_churned=0', (company_id,)).fetchone()[0]:,.2f}")
    print(f"  Transactions : {conn.execute('SELECT COUNT(*) FROM transactions WHERE company_id=?', (company_id,)).fetchone()[0]}")
    print(f"  Events       : {conn.execute('SELECT COUNT(*) FROM engagement_events WHERE company_id=?', (company_id,)).fetchone()[0]}")
    print(f"────────────────────────────────────────────────────\n")
    conn.close()


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="EZER — Salesforce Connector")
    parser.add_argument("--sync",          action="store_true", help="Sync Salesforce data to EZER")
    parser.add_argument("--test",          action="store_true", help="Test Salesforce connection")
    parser.add_argument("--status",        action="store_true", help="Show DB status")
    parser.add_argument("--no-activities", action="store_true", help="Skip activity fetch")
    parser.add_argument("--company-id",    default="default",   help="Company/tenant ID")
    args = parser.parse_args()

    if args.test:
        test_connection()
    elif args.sync:
        if not SF_USERNAME or not SF_PASSWORD:
            print("❌ Salesforce credentials not set.")
            print("   export SALESFORCE_USERNAME=you@company.com")
            print("   export SALESFORCE_PASSWORD=yourpassword")
            print("   export SALESFORCE_SECURITY_TOKEN=yourtoken")
            sys.exit(1)
        sync_salesforce(
            company_id=args.company_id,
            fetch_activities_flag=not args.no_activities,
        )
    elif args.status:
        show_status(args.company_id)
    else:
        parser.print_help()
