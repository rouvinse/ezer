"""
ACIA — Feature Engineering
Pulls raw CRM data from SQLite and builds feature vectors for ML models.
"""

import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.db import read_sql, get_connection
from utils.tenancy import get_tenant, DEFAULT_TENANT_ID


def build_customer_features(company_id: str | None = None) -> pd.DataFrame:
    """
    Joins all tables to produce one feature-rich row per customer.
    Scoped to the current tenant (or company_id if provided).
    Returns a DataFrame ready for ML training or inference.
    """
    cid = company_id or get_tenant().company_id

    # ── Base customer attributes ──────────────────────────────────────────
    customers = read_sql("SELECT * FROM customers WHERE company_id = ?", params=(cid,))
    customers["signup_date"] = pd.to_datetime(customers["signup_date"])
    customers["tenure_days"] = (datetime.now() - customers["signup_date"]).dt.days
    customers["plan_rank"]   = customers["plan"].map(
        {"Free": 0, "Starter": 1, "Pro": 2, "Enterprise": 3}
    )

    # ── Transaction features ──────────────────────────────────────────────
    txns = read_sql("SELECT * FROM transactions WHERE company_id = ?", params=(cid,))
    txns["txn_date"] = pd.to_datetime(txns["txn_date"])

    txn_feats = txns.groupby("customer_id").agg(
        txn_count        = ("txn_id",     "count"),
        total_spend      = ("amount",     "sum"),
        avg_order_value  = ("amount",     "mean"),
        refund_count     = ("status",     lambda x: (x == "refunded").sum()),
        last_txn_days_ago= ("txn_date",   lambda x: (datetime.now() - x.max()).days),
    ).reset_index()
    txn_feats["refund_rate"] = (
        txn_feats["refund_count"] / txn_feats["txn_count"].clip(lower=1)
    )

    # ── Engagement features ───────────────────────────────────────────────
    events = read_sql("SELECT * FROM engagement_events WHERE company_id = ?", params=(cid,))
    events["event_date"] = pd.to_datetime(events["event_date"])
    now = datetime.now()

    evt_feats = events.groupby("customer_id").agg(
        event_count_total  = ("event_id",        "count"),
        event_count_30d    = ("event_date",       lambda x: (x >= now - pd.Timedelta(days=30)).sum()),
        avg_session_sec    = ("session_duration", "mean"),
        unique_channels    = ("channel",          "nunique"),
        unique_event_types = ("event_type",       "nunique"),
        days_since_last_event = ("event_date",    lambda x: (now - x.max()).days),
    ).reset_index()

    # ── Support ticket features ───────────────────────────────────────────
    tickets = read_sql("SELECT * FROM support_tickets WHERE company_id = ?", params=(cid,))
    tkt_feats = tickets.groupby("customer_id").agg(
        ticket_count      = ("ticket_id",       "count"),
        open_tickets      = ("status",          lambda x: (x == "open").sum()),
        critical_tickets  = ("priority",        lambda x: (x == "critical").sum()),
        avg_sentiment     = ("sentiment_score", "mean"),
        cancellation_risk = ("topic",           lambda x: (x == "cancellation").sum()),
    ).reset_index()
    tkt_feats["unresolved_rate"] = (
        tkt_feats["open_tickets"] / tkt_feats["ticket_count"].clip(lower=1)
    )

    # ── Email engagement features ─────────────────────────────────────────
    emails = read_sql("SELECT * FROM email_log WHERE company_id = ?", params=(cid,))
    email_feats = emails.groupby("customer_id").agg(
        emails_sent    = ("email_id", "count"),
        emails_opened  = ("opened",   "sum"),
        emails_clicked = ("clicked",  "sum"),
    ).reset_index()
    email_feats["open_rate"]  = email_feats["emails_opened"]  / email_feats["emails_sent"].clip(lower=1)
    email_feats["click_rate"] = email_feats["emails_clicked"] / email_feats["emails_sent"].clip(lower=1)


    # ── Merge all feature groups ──────────────────────────────────────────
    df = customers.merge(txn_feats,    on="customer_id", how="left")
    df = df.merge(evt_feats,           on="customer_id", how="left")
    df = df.merge(tkt_feats,           on="customer_id", how="left")
    df = df.merge(email_feats,         on="customer_id", how="left")

    # Fill NaN (customers with no activity in a category)
    numeric_cols = df.select_dtypes(include=[np.number]).columns
    df[numeric_cols] = df[numeric_cols].fillna(0)

    # ── Derived composite features ────────────────────────────────────────
    df["recency_score"] = np.where(
        df["days_since_last_event"] == 0, 100,
        100 - np.clip(df["days_since_last_event"] * 1.5, 0, 100)
    )
    df["engagement_index"] = (
        df["event_count_30d"] * 2 +
        df["avg_session_sec"] / 60 +
        df["open_rate"] * 10 +
        df["click_rate"] * 20
    ).round(2)
    df["risk_index"] = (
        df["cancellation_risk"] * 20 +
        df["critical_tickets"] * 10 +
        df["unresolved_rate"] * 15 +
        df["refund_rate"] * 10
    ).round(2)

    return df


def get_feature_columns() -> dict:
    """Returns the feature column names grouped by category."""
    return {
        "customer":    ["tenure_days", "plan_rank", "mrr", "nps_score",
                        "health_score", "employee_count"],
        "transaction": ["txn_count", "total_spend", "avg_order_value",
                        "refund_rate", "last_txn_days_ago"],
        "engagement":  ["event_count_total", "event_count_30d", "avg_session_sec",
                        "unique_channels", "unique_event_types",
                        "days_since_last_event", "recency_score", "engagement_index"],
        "support":     ["ticket_count", "open_tickets", "critical_tickets",
                        "avg_sentiment", "cancellation_risk", "unresolved_rate"],
        "email":       ["open_rate", "click_rate"],
        "derived":     ["risk_index"],
    }


def get_all_feature_cols() -> list:
    cols = []
    for v in get_feature_columns().values():
        cols.extend(v)
    return cols


if __name__ == "__main__":
    print("Building feature matrix...")
    df = build_customer_features()
    print(f"Shape: {df.shape}")
    print(f"\nFeature columns ({len(get_all_feature_cols())}):")
    for cat, cols in get_feature_columns().items():
        print(f"  {cat:12s}: {cols}")
    print(f"\nChurn rate: {df['is_churned'].mean():.1%}")
    print(f"\nSample:\n{df[['customer_id','plan','mrr','health_score','engagement_index','risk_index','is_churned']].head(10).to_string(index=False)}")
