"""
ACIA — Feature Engineering
Pulls raw CRM data from SQLite/PostgreSQL and builds feature vectors for ML models.

Designed to work with ANY combination of connectors:
  - HubSpot only → CRM features only
  - Stripe only → billing features only
  - HubSpot + Stripe → CRM + billing features
  - All connectors → full 28-feature vector
  Missing features are filled with intelligent defaults, not zeros that crash models.
"""

import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.db import read_sql, get_connection
from utils.tenancy import get_tenant, DEFAULT_TENANT_ID


def _safe_read(sql: str, params: tuple) -> pd.DataFrame:
    """Read SQL safely — return empty DataFrame if table missing or query fails."""
    try:
        df = read_sql(sql, params=params)
        return df if df is not None else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def _safe_numeric(df: pd.DataFrame, cols: list) -> pd.DataFrame:
    """Convert columns to numeric safely, filling NaN with 0."""
    for col in cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
    return df


def _ensure_columns(df: pd.DataFrame, columns: dict) -> pd.DataFrame:
    """Add missing columns with default values."""
    for col, default in columns.items():
        if col not in df.columns:
            df[col] = default
    return df


def build_customer_features(company_id: str | None = None) -> pd.DataFrame:
    """
    Joins all tables to produce one feature-rich row per customer.
    Scoped to the current tenant (or company_id if provided).

    Gracefully handles missing data from any connector combination:
    - If no transactions → billing features default to 0
    - If no events → engagement features default to neutral values
    - If no tickets → support features default to 0
    - If no emails → email features default to 0

    Returns a DataFrame ready for ML training or inference.
    """
    cid = company_id or get_tenant().company_id
    now = datetime.now()

    # ── Base customer attributes ──────────────────────────────────────────
    customers = _safe_read("SELECT * FROM customers WHERE company_id = ?", params=(cid,))

    if customers.empty:
        return pd.DataFrame()

    # Safe date parsing
    customers["signup_date"] = pd.to_datetime(customers["signup_date"], errors="coerce")
    customers["tenure_days"] = (now - customers["signup_date"]).dt.days.fillna(0).astype(int)

    # Plan rank — handle any plan names
    plan_rank_map = {
        "Free": 0, "Starter": 1, "Growth": 2, "Pro": 2,
        "Scale": 3, "Enterprise": 3, "Business": 2, "Basic": 1,
    }
    customers["plan_rank"] = customers["plan"].map(plan_rank_map).fillna(1)

    # Ensure key numeric columns are numeric
    customers = _safe_numeric(customers, ["mrr", "health_score", "nps_score", "employee_count", "is_churned"])
    customers["nps_score"]     = customers["nps_score"].fillna(50)
    customers["health_score"]  = customers["health_score"].fillna(60)
    customers["employee_count"]= customers["employee_count"].fillna(0)

    # ── Transaction features ──────────────────────────────────────────────
    txns = _safe_read("SELECT * FROM transactions WHERE company_id = ?", params=(cid,))

    if not txns.empty:
        txns["txn_date"] = pd.to_datetime(txns["txn_date"], errors="coerce")
        txns = _safe_numeric(txns, ["amount"])

        txn_feats = txns.groupby("customer_id").agg(
            txn_count        = ("txn_id",   "count"),
            total_spend      = ("amount",   "sum"),
            avg_order_value  = ("amount",   "mean"),
            refund_count     = ("status",   lambda x: (x == "refunded").sum()),
            last_txn_days_ago= ("txn_date", lambda x: (now - x.max()).days if pd.notna(x.max()) else 999),
        ).reset_index()
        txn_feats["refund_rate"] = (
            txn_feats["refund_count"] / txn_feats["txn_count"].clip(lower=1)
        )
    else:
        # No transaction data — use MRR from customer record as proxy
        txn_feats = customers[["customer_id", "mrr"]].copy()
        txn_feats["txn_count"]         = np.where(txn_feats["mrr"] > 0, 1, 0)
        txn_feats["total_spend"]       = txn_feats["mrr"]
        txn_feats["avg_order_value"]   = txn_feats["mrr"]
        txn_feats["refund_count"]      = 0
        txn_feats["refund_rate"]       = 0.0
        txn_feats["last_txn_days_ago"] = 30
        txn_feats = txn_feats.drop(columns=["mrr"])

    # ── Engagement features ───────────────────────────────────────────────
    events = _safe_read("SELECT * FROM engagement_events WHERE company_id = ?", params=(cid,))

    if not events.empty:
        events["event_date"] = pd.to_datetime(events["event_date"], errors="coerce")
        events = events.dropna(subset=["event_date"])

        evt_feats = events.groupby("customer_id").agg(
            event_count_total     = ("event_id",        "count"),
            event_count_30d       = ("event_date",      lambda x: (x >= now - pd.Timedelta(days=30)).sum()),
            avg_session_sec       = ("session_duration","mean"),
            unique_channels       = ("channel",         "nunique"),
            unique_event_types    = ("event_type",      "nunique"),
            days_since_last_event = ("event_date",      lambda x: (now - x.max()).days if pd.notna(x.max()) else 999),
        ).reset_index()
        evt_feats = _safe_numeric(evt_feats, ["event_count_total", "event_count_30d",
                                               "avg_session_sec", "unique_channels",
                                               "unique_event_types", "days_since_last_event"])
    else:
        # No event data — derive from health score
        evt_feats = customers[["customer_id", "health_score"]].copy()
        evt_feats["event_count_total"]     = np.where(evt_feats["health_score"] > 50, 10, 2)
        evt_feats["event_count_30d"]       = np.where(evt_feats["health_score"] > 50, 3, 0)
        evt_feats["avg_session_sec"]       = np.where(evt_feats["health_score"] > 50, 300, 60)
        evt_feats["unique_channels"]       = 1
        evt_feats["unique_event_types"]    = 1
        evt_feats["days_since_last_event"] = np.where(evt_feats["health_score"] > 70, 5,
                                             np.where(evt_feats["health_score"] > 40, 20, 60))
        evt_feats = evt_feats.drop(columns=["health_score"])

    # ── Support ticket features ───────────────────────────────────────────
    tickets = _safe_read("SELECT * FROM support_tickets WHERE company_id = ?", params=(cid,))

    if not tickets.empty:
        tkt_feats = tickets.groupby("customer_id").agg(
            ticket_count      = ("ticket_id",       "count"),
            open_tickets      = ("status",          lambda x: (x == "open").sum()),
            critical_tickets  = ("priority",        lambda x: (x == "critical").sum()),
            avg_sentiment     = ("sentiment_score", "mean"),
            cancellation_risk = ("topic",           lambda x: x.str.lower().str.contains("cancel", na=False).sum()),
        ).reset_index()
        tkt_feats["unresolved_rate"] = (
            tkt_feats["open_tickets"] / tkt_feats["ticket_count"].clip(lower=1)
        )
        tkt_feats = _safe_numeric(tkt_feats, ["ticket_count", "open_tickets",
                                               "critical_tickets", "avg_sentiment",
                                               "cancellation_risk", "unresolved_rate"])
    else:
        # No ticket data — default to neutral
        tkt_feats = customers[["customer_id"]].copy()
        tkt_feats["ticket_count"]      = 0
        tkt_feats["open_tickets"]      = 0
        tkt_feats["critical_tickets"]  = 0
        tkt_feats["avg_sentiment"]     = 0.0
        tkt_feats["cancellation_risk"] = 0
        tkt_feats["unresolved_rate"]   = 0.0

    # ── Email engagement features ─────────────────────────────────────────
    emails = _safe_read("SELECT * FROM email_log WHERE company_id = ?", params=(cid,))

    if not emails.empty:
        email_feats = emails.groupby("customer_id").agg(
            emails_sent    = ("email_id", "count"),
            emails_opened  = ("opened",   "sum"),
            emails_clicked = ("clicked",  "sum"),
        ).reset_index()
        email_feats = _safe_numeric(email_feats, ["emails_sent", "emails_opened", "emails_clicked"])
        email_feats["open_rate"]  = email_feats["emails_opened"]  / email_feats["emails_sent"].clip(lower=1)
        email_feats["click_rate"] = email_feats["emails_clicked"] / email_feats["emails_sent"].clip(lower=1)
    else:
        # No email data — derive from health score
        email_feats = customers[["customer_id", "health_score"]].copy()
        email_feats["emails_sent"]    = 3
        email_feats["emails_opened"]  = np.where(email_feats["health_score"] > 60, 2, 0)
        email_feats["emails_clicked"] = np.where(email_feats["health_score"] > 70, 1, 0)
        email_feats["open_rate"]      = email_feats["emails_opened"] / 3
        email_feats["click_rate"]     = email_feats["emails_clicked"] / 3
        email_feats = email_feats.drop(columns=["health_score"])

    # ── Merge all feature groups ──────────────────────────────────────────
    df = customers.merge(txn_feats,   on="customer_id", how="left")
    df = df.merge(evt_feats,          on="customer_id", how="left")
    df = df.merge(tkt_feats,          on="customer_id", how="left")
    df = df.merge(email_feats,        on="customer_id", how="left")

    # Fill any remaining NaN
    numeric_cols = df.select_dtypes(include=[np.number]).columns
    df[numeric_cols] = df[numeric_cols].fillna(0)

    # Ensure all expected columns exist
    expected = {
        "txn_count": 0, "total_spend": 0, "avg_order_value": 0,
        "refund_rate": 0, "last_txn_days_ago": 30,
        "event_count_total": 0, "event_count_30d": 0, "avg_session_sec": 0,
        "unique_channels": 0, "unique_event_types": 0, "days_since_last_event": 30,
        "ticket_count": 0, "open_tickets": 0, "critical_tickets": 0,
        "avg_sentiment": 0, "cancellation_risk": 0, "unresolved_rate": 0,
        "open_rate": 0, "click_rate": 0,
    }
    df = _ensure_columns(df, expected)

    # Convert all numeric cols to float
    all_numeric = list(expected.keys()) + ["tenure_days", "plan_rank", "mrr",
                                            "nps_score", "health_score", "employee_count"]
    df = _safe_numeric(df, all_numeric)

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
