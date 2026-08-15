"""
ACIA — Synthetic CRM Data Generator
Generates realistic customer data for all downstream ML + agent layers.
"""

import sys
import random
import json
from datetime import datetime, timedelta
from pathlib import Path

random.seed(42)

# ── Config ──────────────────────────────────────────────────────────────────
NUM_CUSTOMERS   = 500
NUM_PRODUCTS    = 30
sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.db import get_connection, get_schema

INDUSTRIES      = ["Retail", "SaaS", "Finance", "Healthcare", "Education", "Logistics"]
PLANS           = ["Free", "Starter", "Pro", "Enterprise"]
CHANNELS        = ["Email", "SMS", "Push", "In-App", "Sales Call"]
EVENT_TYPES     = ["login", "page_view", "feature_used", "report_exported",
                   "support_opened", "billing_viewed", "upgrade_clicked", "downgrade_clicked"]
TICKET_STATUSES = ["open", "in_progress", "resolved", "escalated"]
TICKET_TOPICS   = ["billing", "technical", "onboarding", "feature_request", "cancellation"]
EMAIL_TYPES     = ["welcome", "nurture", "re_engagement", "upsell", "retention"]

# ── Helpers ──────────────────────────────────────────────────────────────────
def rand_date(start_days_ago: int, end_days_ago: int = 0) -> str:
    delta = random.randint(end_days_ago, start_days_ago)
    return (datetime.now() - timedelta(days=delta)).strftime("%Y-%m-%d %H:%M:%S")

def rand_bool(prob_true: float = 0.5) -> int:
    return 1 if random.random() < prob_true else 0

def weighted_choice(options: list, weights: list):
    return random.choices(options, weights=weights, k=1)[0]

# ── Schema ────────────────────────────────────────────────────────────────────
SCHEMA = """
-- Drop and recreate tables to ensure correct schema
DROP TABLE IF EXISTS ml_predictions CASCADE;
DROP TABLE IF EXISTS agent_actions CASCADE;
DROP TABLE IF EXISTS email_log CASCADE;
DROP TABLE IF EXISTS support_tickets CASCADE;
DROP TABLE IF EXISTS engagement_events CASCADE;
DROP TABLE IF EXISTS transactions CASCADE;
DROP TABLE IF EXISTS customers CASCADE;

CREATE TABLE IF NOT EXISTS customers (
    customer_id     TEXT PRIMARY KEY,
    name            TEXT,
    email           TEXT,
    company         TEXT,
    industry        TEXT,
    plan            TEXT,
    mrr             REAL,
    signup_date     TEXT,
    country         TEXT,
    employee_count  INTEGER,
    health_score    REAL,
    nps_score       INTEGER,
    is_churned      INTEGER,
    churn_date      TEXT,
    created_at      TEXT,
    company_id      TEXT DEFAULT 'default'
);

CREATE TABLE IF NOT EXISTS transactions (
    txn_id          TEXT PRIMARY KEY,
    customer_id     TEXT,
    amount          REAL,
    currency        TEXT DEFAULT 'USD',
    txn_type        TEXT,           -- purchase / renewal / upgrade / refund
    product_id      TEXT,
    status          TEXT,           -- completed / failed / refunded
    txn_date        TEXT,
    FOREIGN KEY(customer_id) REFERENCES customers(customer_id)
    company_id      TEXT DEFAULT 'default'
);

CREATE TABLE IF NOT EXISTS engagement_events (
    event_id        TEXT PRIMARY KEY,
    customer_id     TEXT,
    event_type      TEXT,
    channel         TEXT,
    session_duration INTEGER,       -- seconds
    event_date      TEXT,
    metadata        TEXT,           -- JSON blob
    FOREIGN KEY(customer_id) REFERENCES customers(customer_id)
    company_id      TEXT DEFAULT 'default'
);

CREATE TABLE IF NOT EXISTS support_tickets (
    ticket_id       TEXT PRIMARY KEY,
    customer_id     TEXT,
    topic           TEXT,
    status          TEXT,
    priority        TEXT,           -- low / medium / high / critical
    sentiment_score REAL,           -- -1 to 1 (negative to positive)
    opened_at       TEXT,
    resolved_at     TEXT,
    FOREIGN KEY(customer_id) REFERENCES customers(customer_id)
    company_id      TEXT DEFAULT 'default'
);

CREATE TABLE IF NOT EXISTS email_log (
    email_id        TEXT PRIMARY KEY,
    customer_id     TEXT,
    email_type      TEXT,
    subject         TEXT,
    opened          INTEGER,
    clicked         INTEGER,
    sent_at         TEXT,
    FOREIGN KEY(customer_id) REFERENCES customers(customer_id)
    company_id      TEXT DEFAULT 'default'
);

CREATE TABLE IF NOT EXISTS agent_actions (
    action_id       TEXT PRIMARY KEY,
    customer_id     TEXT,
    action_type     TEXT,
    reason          TEXT,           -- LLM-generated explanation
    priority        INTEGER,        -- 1 (low) to 5 (critical)
    status          TEXT DEFAULT 'pending',  -- pending / executed / skipped
    outcome         TEXT,
    created_at      TEXT,
    executed_at     TEXT,
    FOREIGN KEY(customer_id) REFERENCES customers(customer_id)
    company_id      TEXT DEFAULT 'default'
);

CREATE TABLE IF NOT EXISTS ml_predictions (
    prediction_id   TEXT PRIMARY KEY,
    customer_id     TEXT,
    churn_score     REAL,           -- 0–1
    conversion_score REAL,          -- 0–1
    segment         TEXT,           -- Champion / Loyal / At-Risk / Lost / Prospect
    ltv_estimate    REAL,           -- lifetime value estimate ($)
    predicted_at    TEXT,
    FOREIGN KEY(customer_id) REFERENCES customers(customer_id)
    company_id      TEXT DEFAULT 'default'
);
"""

# ── Generators ────────────────────────────────────────────────────────────────
def gen_customers(n: int) -> list[dict]:
    customers = []
    countries = ["US", "IN", "UK", "DE", "AU", "CA", "SG", "FR", "BR", "JP"]
    plan_mrr  = {"Free": 0, "Starter": 49, "Pro": 199, "Enterprise": 999}
    plan_w    = [0.30, 0.35, 0.25, 0.10]

    for i in range(1, n + 1):
        plan        = weighted_choice(PLANS, plan_w)
        mrr_base    = plan_mrr[plan]
        mrr         = round(mrr_base * random.uniform(0.9, 1.4), 2)
        signup_days = random.randint(30, 730)
        signup_date = rand_date(signup_days, signup_days)
        is_churned  = rand_bool(0.20)          # 20% churn rate
        churn_date  = rand_date(signup_days // 2, 10) if is_churned else None

        # Health score: lower if churned or on Free
        base_health = random.uniform(30, 95)
        if is_churned:
            base_health *= random.uniform(0.3, 0.6)
        if plan == "Free":
            base_health *= random.uniform(0.7, 1.0)
        health_score = round(min(max(base_health, 0), 100), 1)

        customers.append({
            "customer_id":    f"C{i:04d}",
            "name":           f"Customer {i:04d}",
            "email":          f"customer{i:04d}@example.com",
            "company":        f"Company {i:04d}",
            "industry":       random.choice(INDUSTRIES),
            "plan":           plan,
            "mrr":            mrr,
            "signup_date":    signup_date,
            "country":        random.choice(countries),
            "employee_count": random.choice([1,5,10,25,50,100,250,500,1000]),
            "health_score":   health_score,
            "nps_score":      random.randint(-100, 100),
            "is_churned":     is_churned,
            "churn_date":     churn_date,
            "created_at":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
    return customers


def gen_transactions(customers: list[dict]) -> list[dict]:
    txns = []
    txn_types = ["purchase", "renewal", "upgrade", "refund"]
    type_w    = [0.40, 0.40, 0.15, 0.05]

    for cust in customers:
        n_txns = random.randint(0, 20) if cust["plan"] != "Free" else random.randint(0, 3)
        for j in range(n_txns):
            txn_type = weighted_choice(txn_types, type_w)
            amount   = round(abs(random.gauss(cust["mrr"] or 10, 30)), 2)
            if txn_type == "refund":
                amount = -amount
            txns.append({
                "txn_id":     f"T{len(txns)+1:06d}",
                "customer_id": cust["customer_id"],
                "amount":      amount,
                "currency":    "USD",
                "txn_type":    txn_type,
                "product_id":  f"P{random.randint(1, NUM_PRODUCTS):03d}",
                "status":      weighted_choice(["completed","failed","refunded"], [0.88,0.07,0.05]),
                "txn_date":    rand_date(365),
            })
    return txns


def gen_events(customers: list[dict]) -> list[dict]:
    events = []
    for cust in customers:
        # Churned customers have very few recent events
        n_events = random.randint(1, 5) if cust["is_churned"] else random.randint(5, 60)
        for _ in range(n_events):
            events.append({
                "event_id":        f"E{len(events)+1:07d}",
                "customer_id":     cust["customer_id"],
                "event_type":      random.choice(EVENT_TYPES),
                "channel":         random.choice(CHANNELS),
                "session_duration": random.randint(30, 3600),
                "event_date":      rand_date(90),
                "metadata":        json.dumps({"source": random.choice(["web", "mobile", "api"])}),
            })
    return events


def gen_tickets(customers: list[dict]) -> list[dict]:
    tickets = []
    priorities = ["low","medium","high","critical"]
    p_weights  = [0.4, 0.3, 0.2, 0.1]

    for cust in customers:
        # At-risk customers open more tickets
        n_tickets = random.randint(2, 8) if cust["health_score"] < 40 else random.randint(0, 3)
        for _ in range(n_tickets):
            opened = rand_date(180)
            status = weighted_choice(TICKET_STATUSES, [0.15, 0.10, 0.65, 0.10])
            days_open = int((datetime.now() - datetime.strptime(opened, "%Y-%m-%d %H:%M:%S")).days)
            resolved = rand_date(max(days_open, 1), 0) if status == "resolved" else None
            tickets.append({
                "ticket_id":       f"TK{len(tickets)+1:05d}",
                "customer_id":     cust["customer_id"],
                "topic":           random.choice(TICKET_TOPICS),
                "status":          status,
                "priority":        weighted_choice(priorities, p_weights),
                "sentiment_score": round(random.uniform(-1, 1), 3),
                "opened_at":       opened,
                "resolved_at":     resolved,
            })
    return tickets


def gen_emails(customers: list[dict]) -> list[dict]:
    emails = []
    for cust in customers:
        n_emails = random.randint(1, 12)
        for _ in range(n_emails):
            etype = random.choice(EMAIL_TYPES)
            opened = rand_bool(0.35)
            emails.append({
                "email_id":    f"EM{len(emails)+1:06d}",
                "customer_id": cust["customer_id"],
                "email_type":  etype,
                "subject":     f"{etype.replace('_',' ').title()} — {cust['company']}",
                "opened":      opened,
                "clicked":     rand_bool(0.15) if opened else 0,
                "sent_at":     rand_date(180),
            })
    return emails


# ── Build DB ──────────────────────────────────────────────────────────────────
def _insert_many(conn, table: str, cols: list, rows: list):
    """
    Engine-agnostic bulk insert.
    SQLite uses named :col placeholders, PostgreSQL uses %s positional.
    """
    from utils.db import DB_ENGINE
    if DB_ENGINE == "postgresql":
        placeholders = ", ".join(["%s"] * len(cols))
        sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"
        data = [tuple(r[c] for c in cols) for r in rows]
    else:
        placeholders = ", ".join([f":{c}" for c in cols])
        sql = f"INSERT OR REPLACE INTO {table} VALUES ({placeholders})"
        data = rows
    conn.executemany(sql, data)


def build_database():
    from utils.db import DB_ENGINE, get_schema
    print("🏗  Building ACIA database...")

    conn = get_connection()
    conn.executescript(get_schema())
    conn.commit()

    DEFAULT_CID = "default"
    print(f"  Generating {NUM_CUSTOMERS} customers...")
    customers = gen_customers(NUM_CUSTOMERS)
    for r in customers: r["company_id"] = DEFAULT_CID
    _insert_many(conn, "customers",
        ["customer_id","name","email","company","industry","plan","mrr","signup_date",
         "country","employee_count","health_score","nps_score","is_churned","churn_date","created_at","company_id"],
        customers)

    print("  Generating transactions...")
    txns = gen_transactions(customers)
    for r in txns: r["company_id"] = DEFAULT_CID
    _insert_many(conn, "transactions",
        ["txn_id","customer_id","amount","currency","txn_type","product_id","status","txn_date","company_id"],
        txns)

    print("  Generating engagement events...")
    events = gen_events(customers)
    for r in events: r["company_id"] = DEFAULT_CID
    _insert_many(conn, "engagement_events",
        ["event_id","customer_id","event_type","channel","session_duration","event_date","metadata","company_id"],
        events)

    print("  Generating support tickets...")
    tickets = gen_tickets(customers)
    for r in tickets: r["company_id"] = DEFAULT_CID
    _insert_many(conn, "support_tickets",
        ["ticket_id","customer_id","topic","status","priority","sentiment_score","opened_at","resolved_at","company_id"],
        tickets)

    print("  Generating email log...")
    emails = gen_emails(customers)
    for r in emails: r["company_id"] = DEFAULT_CID
    _insert_many(conn, "email_log",
        ["email_id","customer_id","email_type","subject","opened","clicked","sent_at","company_id"],
        emails)

    conn.commit()
    conn.close()
    print(f"\n✅ Database built ({DB_ENGINE.upper()})")
    return customers, txns, events, tickets, emails


def print_summary(customers, txns, events, tickets, emails):
    churned     = sum(1 for c in customers if c["is_churned"])
    plan_counts = {}
    for c in customers:
        plan_counts[c["plan"]] = plan_counts.get(c["plan"], 0) + 1
    total_mrr   = sum(c["mrr"] for c in customers if not c["is_churned"])

    print("\n── Data Summary ──────────────────────────────────")
    print(f"  Customers      : {len(customers):,}  ({churned} churned, {len(customers)-churned} active)")
    print(f"  Transactions   : {len(txns):,}")
    print(f"  Events         : {len(events):,}")
    print(f"  Tickets        : {len(tickets):,}")
    print(f"  Emails         : {len(emails):,}")
    print(f"  Total MRR      : ${total_mrr:,.2f}")
    print(f"  Plan breakdown : { {k: plan_counts.get(k,0) for k in PLANS} }")
    print("──────────────────────────────────────────────────\n")


if __name__ == "__main__":
    customers, txns, events, tickets, emails = build_database()
    print_summary(customers, txns, events, tickets, emails)
