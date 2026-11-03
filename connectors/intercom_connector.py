"""
EZER — Intercom Connector
=========================
Pulls customer conversation and engagement data from Intercom
and maps it into EZER's database schema.

What it pulls:
  - Contacts (customers — name, email, company, last seen)
  - Conversations (support tickets — topic, sentiment, status)
  - Events (product usage — logins, feature use, activity)
  - Companies (company details — industry, employee count, MRR)

What it maps to:
  - customers table (from contacts + companies)
  - support_tickets table (from conversations)
  - engagement_events table (from events)

Setup:
  1. Go to app.intercom.com → Settings → Integrations → Developer Hub
  2. Create an app → Basic Information → Access Token
  3. Set: export INTERCOM_ACCESS_TOKEN=dG9rOj...
  4. Run: python connectors/intercom_connector.py --sync

Usage:
  python connectors/intercom_connector.py --test
  python connectors/intercom_connector.py --sync --company-id YOUR_ID
  python connectors/intercom_connector.py --status --company-id YOUR_ID
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

INTERCOM_BASE_URL = "https://api.intercom.io"


def _make_headers(access_token: str) -> dict:
    """Build authenticated headers for Intercom API."""
    if not access_token:
        raise ValueError(
            "Intercom access token not provided.\n"
            "Pass access_token= to sync_intercom(), or set INTERCOM_ACCESS_TOKEN "
            "in your environment for CLI use.\n"
            "Get it from: app.intercom.com → Settings → Integrations → Developer Hub"
        )
    return {
        "Authorization":    f"Bearer {access_token}",
        "Accept":           "application/json",
        "Content-Type":     "application/json",
        "Intercom-Version": "2.10",
    }


# ── Connection ─────────────────────────────────────────────────────────────────

def test_connection(access_token: str = None) -> bool:
    """Test Intercom API connection."""
    token = access_token or os.environ.get("INTERCOM_ACCESS_TOKEN", "")
    try:
        response = requests.get(f"{INTERCOM_BASE_URL}/me", headers=_make_headers(token))
        response.raise_for_status()
        data = response.json()
        print(f"\n✅ Intercom connected")
        print(f"   App     : {data.get('app', {}).get('name', 'Unknown')}")
        print(f"   Token   : {token[:15]}...\n")
        return True
    except Exception as e:
        print(f"\n❌ Intercom connection failed: {e}\n")
        return False


# ── Fetchers ───────────────────────────────────────────────────────────────────

def intercom_get(endpoint: str, access_token: str, params: dict = None) -> dict:
    """Make authenticated GET request to Intercom."""
    url = f"{INTERCOM_BASE_URL}/{endpoint}"
    response = requests.get(url, headers=_make_headers(access_token), params=params or {})
    response.raise_for_status()
    return response.json()


def intercom_post(endpoint: str, access_token: str, payload: dict) -> dict:
    """Make authenticated POST request to Intercom (for search)."""
    url = f"{INTERCOM_BASE_URL}/{endpoint}"
    response = requests.post(url, headers=_make_headers(access_token), json=payload)
    response.raise_for_status()
    return response.json()


def fetch_contacts(access_token: str, limit: int = 150) -> list[dict]:
    """Fetch all contacts from Intercom with pagination."""
    contacts = []
    starting_after = None

    print("  Fetching contacts from Intercom...")
    while True:
        params = {"per_page": min(limit, 150)}
        if starting_after:
            params["starting_after"] = starting_after

        result = intercom_get("contacts", access_token, params)
        batch  = result.get("data", [])
        contacts.extend(batch)

        pages = result.get("pages", {})
        next_page = pages.get("next", {})
        starting_after = next_page.get("starting_after") if next_page else None

        if not starting_after or len(batch) < params["per_page"]:
            break

    print(f"  Found {len(contacts)} contacts")
    return contacts


def fetch_conversations(access_token: str, limit: int = 150) -> list[dict]:
    """Fetch all conversations from Intercom."""
    conversations = []
    starting_after = None

    print("  Fetching conversations from Intercom...")
    while True:
        params = {"per_page": min(limit, 150), "order": "desc", "sort": "updated_at"}
        if starting_after:
            params["starting_after"] = starting_after

        result = intercom_get("conversations", access_token, params)
        batch  = result.get("conversations", [])
        conversations.extend(batch)

        pages = result.get("pages", {})
        next_page = pages.get("next", {})
        starting_after = next_page.get("starting_after") if next_page else None

        if not starting_after or len(batch) < params["per_page"]:
            break

    print(f"  Found {len(conversations)} conversations")
    return conversations


def fetch_events_for_contact(contact_id: str, access_token: str) -> list[dict]:
    """Fetch events for a specific contact."""
    try:
        result = intercom_get("events", access_token, {
            "type": "user",
            "intercom_user_id": contact_id,
            "per_page": 50,
        })
        return result.get("events", [])
    except Exception:
        return []


# ── Sentiment analysis ─────────────────────────────────────────────────────────

def analyze_sentiment(text: str) -> float:
    """
    Simple keyword-based sentiment scoring.
    Returns score from -1.0 (very negative) to +1.0 (very positive).
    """
    if not text:
        return 0.0

    text_lower = text.lower()

    negative_words = [
        "broken", "not working", "bug", "error", "crash", "terrible",
        "awful", "hate", "worst", "useless", "cancel", "refund", "angry",
        "frustrated", "disappointed", "failed", "issue", "problem", "urgent",
        "immediately", "unacceptable", "ridiculous", "horrible", "awful"
    ]

    positive_words = [
        "great", "awesome", "excellent", "love", "perfect", "amazing",
        "helpful", "fantastic", "wonderful", "thank", "thanks", "good",
        "nice", "brilliant", "superb", "happy", "pleased", "satisfied"
    ]

    neg_count = sum(1 for w in negative_words if w in text_lower)
    pos_count = sum(1 for w in positive_words if w in text_lower)

    if neg_count == 0 and pos_count == 0:
        return 0.0

    total = neg_count + pos_count
    score = (pos_count - neg_count) / total
    return round(score, 2)


# ── Mappers ────────────────────────────────────────────────────────────────────

def map_contact_to_customer(
    contact: dict,
    company_id: str,
) -> dict:
    """Map an Intercom contact to EZER's customer schema."""
    now = datetime.now(timezone.utc).isoformat()

    name  = contact.get("name") or ""
    email = contact.get("email") or ""
    if not name:
        name = email.split("@")[0] if email else "Unknown"

    company_data = contact.get("companies", {}).get("data", [])
    company_name = ""
    industry     = "Unknown"
    employee_count = 0
    mrr          = 0.0

    if company_data:
        first_company = company_data[0]
        company_name  = first_company.get("name", "") or ""
        industry      = first_company.get("industry", "Unknown") or "Unknown"
        employee_count = first_company.get("size", 0) or 0
        mrr           = float(first_company.get("monthly_spend", 0) or 0)

    custom_attrs = contact.get("custom_attributes", {}) or {}
    plan = custom_attrs.get("plan") or custom_attrs.get("subscription_plan") or "Starter"

    created_at = contact.get("created_at", 0) or 0
    signup_date = datetime.fromtimestamp(
        created_at, tz=timezone.utc
    ).strftime("%Y-%m-%d") if created_at else now[:10]

    last_seen_at = contact.get("last_seen_at", 0) or 0
    health_score = 60.0

    if last_seen_at:
        days_since = (datetime.now(timezone.utc).timestamp() - last_seen_at) / 86400
        if days_since < 7:
            health_score = 80.0
        elif days_since < 14:
            health_score = 70.0
        elif days_since < 30:
            health_score = 55.0
        elif days_since < 60:
            health_score = 35.0
        else:
            health_score = 20.0

    is_churned = 0
    churn_date = None
    archived   = contact.get("archived", False)

    if archived or (last_seen_at and (datetime.now(timezone.utc).timestamp() - last_seen_at) / 86400 > 90):
        is_churned = 1
        health_score = 10.0
        if last_seen_at:
            churn_date = datetime.fromtimestamp(last_seen_at, tz=timezone.utc).strftime("%Y-%m-%d")

    return {
        "customer_id":    str(uuid.uuid4()),
        "intercom_id":    contact.get("id", ""),
        "name":           name[:200],
        "email":          email[:200],
        "company":        company_name[:200] or name,
        "industry":       industry[:100],
        "plan":           plan[:50],
        "mrr":            mrr,
        "signup_date":    signup_date,
        "country":        contact.get("location", {}).get("country", "Unknown") or "Unknown",
        "employee_count": employee_count,
        "health_score":   round(health_score, 1),
        "nps_score":      None,
        "is_churned":     is_churned,
        "churn_date":     churn_date,
        "created_at":     now,
        "company_id":     company_id,
    }


def map_conversation_to_ticket(
    conversation: dict,
    customer_id:  str,
    company_id:   str,
) -> dict | None:
    """Map an Intercom conversation to EZER's support_tickets schema."""
    if not conversation.get("id"):
        return None

    source       = conversation.get("source", {}) or {}
    first_msg    = source.get("body", "") or ""
    topic        = first_msg[:100] if first_msg else "General inquiry"

    state = conversation.get("state", "open")
    status_map = {
        "open":    "open",
        "closed":  "resolved",
        "snoozed": "pending",
    }
    status = status_map.get(state, "open")

    tags      = conversation.get("tags", {}).get("tags", [])
    tag_names = [t.get("name", "").lower() for t in tags]
    if any(t in tag_names for t in ["urgent", "critical", "p0", "p1"]):
        priority = "high"
    elif any(t in tag_names for t in ["billing", "churn", "cancel"]):
        priority = "high"
    else:
        priority = "medium"

    sentiment_score = analyze_sentiment(first_msg)

    created_ts  = conversation.get("created_at", 0) or 0
    updated_ts  = conversation.get("updated_at", 0) or 0
    opened_at   = datetime.fromtimestamp(created_ts, tz=timezone.utc).strftime(
        "%Y-%m-%d"
    ) if created_ts else datetime.now().strftime("%Y-%m-%d")
    resolved_at = datetime.fromtimestamp(updated_ts, tz=timezone.utc).strftime(
        "%Y-%m-%d"
    ) if (updated_ts and state == "closed") else None

    return {
        "ticket_id":       str(uuid.uuid4()),
        "intercom_id":     conversation.get("id", ""),
        "customer_id":     customer_id,
        "topic":           topic[:200],
        "status":          status,
        "priority":        priority,
        "sentiment_score": sentiment_score,
        "opened_at":       opened_at,
        "resolved_at":     resolved_at,
        "company_id":      company_id,
    }


def map_event_to_engagement(
    event:       dict,
    customer_id: str,
    company_id:  str,
) -> dict | None:
    """Map an Intercom event to EZER's engagement_events schema."""
    event_name = event.get("event_name", "") or ""
    if not event_name:
        return None

    if any(x in event_name.lower() for x in ["login", "signin", "sign_in", "session"]):
        event_type = "login"
    elif any(x in event_name.lower() for x in ["feature", "click", "use", "action"]):
        event_type = "feature_use"
    elif any(x in event_name.lower() for x in ["page", "view", "visit"]):
        event_type = "page_view"
    elif any(x in event_name.lower() for x in ["export", "download"]):
        event_type = "export"
    else:
        event_type = "other"

    ts = event.get("created_at", 0) or 0
    event_date = datetime.fromtimestamp(
        ts, tz=timezone.utc
    ).strftime("%Y-%m-%d") if ts else datetime.now().strftime("%Y-%m-%d")

    return {
        "event_id":        str(uuid.uuid4()),
        "customer_id":     customer_id,
        "event_type":      event_type,
        "channel":         "intercom",
        "session_duration": 0,
        "event_date":      event_date,
        "metadata":        event_name[:100],
        "company_id":      company_id,
    }


# ── DB writer ──────────────────────────────────────────────────────────────────

def write_to_db(
    customers:  list[dict],
    tickets:    list[dict],
    events:     list[dict],
    company_id: str,
):
    """Write Intercom data to EZER database."""
    from utils.db import get_connection, DB_ENGINE

    conn = get_connection()

    print("  Clearing existing data for fresh sync...")
    conn.execute("DELETE FROM support_tickets WHERE company_id=?", (company_id,))
    conn.execute("DELETE FROM engagement_events WHERE company_id=?", (company_id,))
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
        ph   = ", ".join(["%s"] * len(customer_cols))
        sql  = f"INSERT INTO customers ({', '.join(customer_cols)}) VALUES ({ph}) ON CONFLICT DO NOTHING"
        data = [tuple(c.get(col) for col in customer_cols) for c in customers]
    else:
        ph   = ", ".join([f":{c}" for c in customer_cols])
        sql  = f"INSERT OR REPLACE INTO customers ({', '.join(customer_cols)}) VALUES ({ph})"
        data = customers
    conn.executemany(sql, data)
    conn.commit()

    ticket_cols = [
        "ticket_id", "customer_id", "topic", "status",
        "priority", "sentiment_score", "opened_at", "resolved_at", "company_id"
    ]
    print(f"  Writing {len(tickets)} support tickets...")
    if tickets:
        if DB_ENGINE == "postgresql":
            ph   = ", ".join(["%s"] * len(ticket_cols))
            sql  = f"INSERT INTO support_tickets ({', '.join(ticket_cols)}) VALUES ({ph}) ON CONFLICT DO NOTHING"
            data = [tuple(t.get(col) for col in ticket_cols) for t in tickets]
        else:
            ph   = ", ".join([f":{c}" for c in ticket_cols])
            sql  = f"INSERT OR REPLACE INTO support_tickets ({', '.join(ticket_cols)}) VALUES ({ph})"
            data = tickets
        conn.executemany(sql, data)
        conn.commit()

    event_cols = [
        "event_id", "customer_id", "event_type", "channel",
        "session_duration", "event_date", "metadata", "company_id"
    ]
    print(f"  Writing {len(events)} engagement events...")
    if events:
        if DB_ENGINE == "postgresql":
            ph   = ", ".join(["%s"] * len(event_cols))
            sql  = f"INSERT INTO engagement_events ({', '.join(event_cols)}) VALUES ({ph}) ON CONFLICT DO NOTHING"
            data = [tuple(e.get(col) for col in event_cols) for e in events]
        else:
            ph   = ", ".join([f":{c}" for c in event_cols])
            sql  = f"INSERT OR REPLACE INTO engagement_events ({', '.join(event_cols)}) VALUES ({ph})"
            data = events
        conn.executemany(sql, data)
        conn.commit()

    conn.close()


# ── Main sync ──────────────────────────────────────────────────────────────────

def sync_intercom(
    company_id:        str,
    access_token:      str = None,
    fetch_events_flag: bool = False,
    verbose:           bool = True,
) -> dict:
    """
    Full Intercom → EZER sync.

    Args:
        company_id:        EZER tenant ID
        access_token:      Intercom access token for this tenant.
                           Do NOT pass None in API context.
        fetch_events_flag: Whether to also pull per-contact events
        verbose:           Print progress
    """
    token = access_token or os.environ.get("INTERCOM_ACCESS_TOKEN", "")
    if not token:
        raise ValueError("Intercom access token not provided.")

    if verbose:
        print(f"\n── Intercom Sync ────────────────────────────────────")
        print(f"  Company ID : {company_id}")
        print()

    errors = []

    if verbose:
        print("[1/4] Fetching data from Intercom...")

    contacts      = fetch_contacts(token)
    conversations = fetch_conversations(token)

    if verbose:
        print(f"\n[2/4] Mapping {len(contacts)} contacts to customers...")

    customers = []
    intercom_id_to_customer = {}

    for contact in contacts:
        try:
            customer = map_contact_to_customer(contact, company_id)
            customers.append(customer)
            intercom_id_to_customer[contact.get("id", "")] = customer
        except Exception as e:
            errors.append(f"Contact {contact.get('id','?')}: {e}")

    if verbose:
        print(f"\n[3/4] Mapping {len(conversations)} conversations to tickets...")

    tickets = []
    for conv in conversations:
        try:
            contacts_list = conv.get("contacts", {}).get("contacts", [])
            customer_id = None

            for c in contacts_list:
                intercom_contact_id = c.get("id", "")
                if intercom_contact_id in intercom_id_to_customer:
                    customer_id = intercom_id_to_customer[intercom_contact_id]["customer_id"]
                    break

            if not customer_id and customers:
                customer_id = customers[0]["customer_id"]

            if customer_id:
                ticket = map_conversation_to_ticket(conv, customer_id, company_id)
                if ticket:
                    tickets.append(ticket)
        except Exception as e:
            errors.append(f"Conversation {conv.get('id','?')}: {e}")

    events = []
    if fetch_events_flag:
        if verbose:
            print(f"\n[4/4] Fetching events for {len(customers[:50])} customers...")
        for customer in customers[:50]:
            try:
                raw_events = fetch_events_for_contact(customer["intercom_id"], token)
                for event in raw_events:
                    mapped = map_event_to_engagement(event, customer["customer_id"], company_id)
                    if mapped:
                        events.append(mapped)
            except Exception as e:
                errors.append(f"Events for {customer['intercom_id']}: {e}")
    else:
        if verbose:
            print(f"\n[4/4] Skipping events (use --events flag to enable)")

    if verbose:
        print(f"\nWriting to database...")

    write_to_db(customers, tickets, events, company_id)

    active    = sum(1 for c in customers if not c["is_churned"])
    churned   = sum(1 for c in customers if c["is_churned"])
    total_mrr = sum(c["mrr"] for c in customers)

    summary = {
        "company_id":   company_id,
        "customers":    len(customers),
        "active":       active,
        "churned":      churned,
        "tickets":      len(tickets),
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
        print(f"  Support tickets   : {len(tickets)}")
        print(f"  Engagement events : {len(events)}")
        print(f"  Total MRR         : ${total_mrr:,.2f}")
        if errors:
            print(f"  Errors            : {len(errors)}")
        print(f"────────────────────────────────────────────────────\n")

    return summary


# ── Status ─────────────────────────────────────────────────────────────────────

def show_status(company_id: str):
    from utils.db import get_connection
    conn = get_connection()
    print(f"\n── EZER Database Status (Intercom) ──────────────────")
    print(f"  Customers    : {conn.execute('SELECT COUNT(*) FROM customers WHERE company_id=?', (company_id,)).fetchone()[0]}")
    print(f"  Active       : {conn.execute('SELECT COUNT(*) FROM customers WHERE company_id=? AND is_churned=0', (company_id,)).fetchone()[0]}")
    print(f"  Tickets      : {conn.execute('SELECT COUNT(*) FROM support_tickets WHERE company_id=?', (company_id,)).fetchone()[0]}")
    print(f"  Events       : {conn.execute('SELECT COUNT(*) FROM engagement_events WHERE company_id=?', (company_id,)).fetchone()[0]}")
    print(f"────────────────────────────────────────────────────\n")
    conn.close()


# ── API endpoints ──────────────────────────────────────────────────────────────

def add_intercom_endpoints(app, require_auth):
    """Add Intercom connector endpoints to FastAPI app."""
    from fastapi import Depends, HTTPException
    from pydantic import BaseModel

    class IntercomSyncRequest(BaseModel):
        fetch_events: bool = False

    @app.post("/connectors/intercom/sync", tags=["Connectors"])
    def intercom_sync(req: IntercomSyncRequest, company: dict = Depends(require_auth)):
        """Sync all Intercom contacts and conversations into EZER."""
        access_token = company.get("intercom_token") or ""
        if not access_token:
            raise HTTPException(status_code=400, detail="Intercom access token not configured for this account.")
        try:
            return sync_intercom(
                company_id=company["company_id"],
                access_token=access_token,
                fetch_events_flag=req.fetch_events,
                verbose=False,
            )
        except Exception:
            raise HTTPException(status_code=500, detail="Intercom sync failed")

    @app.get("/connectors/intercom/status", tags=["Connectors"])
    def intercom_status(company: dict = Depends(require_auth)):
        """Show current Intercom data in EZER database."""
        from utils.db import get_connection
        conn = get_connection()
        cid  = company["company_id"]
        result = {
            "customers": conn.execute("SELECT COUNT(*) FROM customers WHERE company_id=?", (cid,)).fetchone()[0],
            "active":    conn.execute("SELECT COUNT(*) FROM customers WHERE company_id=? AND is_churned=0", (cid,)).fetchone()[0],
            "tickets":   conn.execute("SELECT COUNT(*) FROM support_tickets WHERE company_id=?", (cid,)).fetchone()[0],
            "events":    conn.execute("SELECT COUNT(*) FROM engagement_events WHERE company_id=?", (cid,)).fetchone()[0],
        }
        conn.close()
        return result

    @app.post("/connectors/intercom/test", tags=["Connectors"])
    def intercom_test(company: dict = Depends(require_auth)):
        """Test Intercom API connection."""
        access_token = company.get("intercom_token") or ""
        if not access_token:
            raise HTTPException(status_code=400, detail="Intercom access token not configured.")
        success = test_connection(access_token)
        if not success:
            raise HTTPException(status_code=400, detail="Intercom connection test failed")
        return {"connected": True, "token": access_token[:15] + "..."}


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="EZER — Intercom Connector")
    parser.add_argument("--sync",       action="store_true", help="Sync Intercom data to EZER")
    parser.add_argument("--test",       action="store_true", help="Test Intercom connection")
    parser.add_argument("--status",     action="store_true", help="Show DB status")
    parser.add_argument("--events",     action="store_true", help="Also fetch user events")
    parser.add_argument("--company-id", default="default",   help="Company/tenant ID")
    args = parser.parse_args()

    _cli_token = os.environ.get("INTERCOM_ACCESS_TOKEN", "")

    if args.test:
        test_connection(_cli_token)
    elif args.sync:
        if not _cli_token:
            print("❌ INTERCOM_ACCESS_TOKEN not set.")
            print("   Get it: app.intercom.com → Settings → Integrations → Developer Hub")
            print("   Then: export INTERCOM_ACCESS_TOKEN=dG9rOj...")
            sys.exit(1)
        sync_intercom(
            company_id=args.company_id,
            access_token=_cli_token,
            fetch_events_flag=args.events,
        )
    elif args.status:
        show_status(args.company_id)
    else:
        parser.print_help()
