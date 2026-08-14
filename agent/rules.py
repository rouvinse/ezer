"""
ACIA — Rule Engine
Evaluates each customer against a set of business rules and emits
ActionCandidate objects consumed by the LLM planner and scheduler.
"""

from __future__ import annotations
import pandas as pd
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from data.features import build_customer_features

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.db import get_connection, read_sql
from utils.tenancy import get_tenant, DEFAULT_TENANT_ID


# ── Data model ────────────────────────────────────────────────────────────────
@dataclass
class ActionCandidate:
    customer_id:    str
    action_type:    str          # e.g. "send_retention_email"
    reason:         str          # human-readable trigger explanation
    priority:       int          # 1=low … 5=critical
    rule_id:        str          # which rule fired
    triggered_by:   dict         # snapshot of the signal values that fired
    context:        dict = field(default_factory=dict)  # extra data for LLM


# ── Rules registry ────────────────────────────────────────────────────────────
# Each rule is a function: (row: pd.Series) -> ActionCandidate | None
RULES: list[dict] = []

def rule(rule_id: str, priority: int):
    """Decorator to register a rule function."""
    def decorator(fn):
        RULES.append({"id": rule_id, "priority": priority, "fn": fn})
        return fn
    return decorator


# ─── CHURN RULES ──────────────────────────────────────────────────────────────

@rule("CHN-001", priority=5)
def critical_churn_risk(row):
    """Customer has very high churn probability and significant MRR."""
    if row["churn_score"] >= 0.80 and row["mrr"] >= 100:
        return ActionCandidate(
            customer_id=row["customer_id"],
            action_type="escalate_to_csm",
            reason=(
                f"Critical churn risk ({row['churn_score']:.0%}) on a ${row['mrr']:.0f}/mo account. "
                f"Immediate CSM intervention required."
            ),
            priority=5,
            rule_id="CHN-001",
            triggered_by={
                "churn_score": row["churn_score"],
                "mrr": row["mrr"],
                "segment": row["segment"],
            },
        )


@rule("CHN-002", priority=4)
def high_churn_send_retention(row):
    """High churn risk customer — trigger personalised retention campaign."""
    if 0.55 <= row["churn_score"] < 0.80:
        return ActionCandidate(
            customer_id=row["customer_id"],
            action_type="send_retention_email",
            reason=(
                f"High churn risk ({row['churn_score']:.0%}). "
                f"Segment: {row['segment']}. Send targeted retention offer."
            ),
            priority=4,
            rule_id="CHN-002",
            triggered_by={
                "churn_score": row["churn_score"],
                "segment": row["segment"],
                "health_score": row["health_score"],
            },
        )


@rule("CHN-003", priority=3)
def dormant_re_engagement(row):
    """Customer hasn't engaged in 30+ days — re-engagement campaign."""
    days = pd.to_numeric(row["days_since_last_event"], errors="coerce")
    if pd.notna(days) and days >= 30 and row["churn_score"] >= 0.35:
        return ActionCandidate(
            customer_id=row["customer_id"],
            action_type="send_reengagement_email",
            reason=(
                f"No activity for {int(row['days_since_last_event'])} days. "
                f"Churn score: {row['churn_score']:.0%}. Re-engagement sequence needed."
            ),
            priority=3,
            rule_id="CHN-003",
            triggered_by={
                "days_since_last_event": row["days_since_last_event"],
                "churn_score": row["churn_score"],
                "event_count_30d": row["event_count_30d"],
            },
        )


@rule("CHN-004", priority=3)
def negative_support_sentiment(row):
    """Customer opened cancellation ticket or has very negative support sentiment."""
    if row["cancellation_risk"] >= 1 or (
        row["avg_sentiment"] < -0.4 and row["ticket_count"] >= 2
    ):
        return ActionCandidate(
            customer_id=row["customer_id"],
            action_type="proactive_support_call",
            reason=(
                f"Cancellation intent signal or negative sentiment "
                f"(score={row['avg_sentiment']:.2f}, {int(row['cancellation_risk'])} cancel ticket(s)). "
                f"Schedule proactive support call."
            ),
            priority=4,
            rule_id="CHN-004",
            triggered_by={
                "cancellation_risk": row["cancellation_risk"],
                "avg_sentiment": row["avg_sentiment"],
                "open_tickets": row["open_tickets"],
            },
        )


# ─── CONVERSION RULES ─────────────────────────────────────────────────────────

@rule("CNV-001", priority=4)
def hot_conversion_upsell(row):
    """High-intent Free/Starter customer ripe for upgrade offer."""
    if row["conversion_score"] >= 0.70 and row["plan_rank"] <= 1:
        return ActionCandidate(
            customer_id=row["customer_id"],
            action_type="send_upgrade_offer",
            reason=(
                f"High conversion probability ({row['conversion_score']:.0%}) on {row['plan']} plan. "
                f"Send personalised upgrade offer with incentive."
            ),
            priority=4,
            rule_id="CNV-001",
            triggered_by={
                "conversion_score": row["conversion_score"],
                "plan": row["plan"],
                "engagement_index": row["engagement_index"],
            },
            context={"suggested_plan": "Pro" if row["plan_rank"] == 1 else "Starter"},
        )


@rule("CNV-002", priority=2)
def warm_nurture_sequence(row):
    """Warm prospect — enroll in nurture sequence."""
    if 0.40 <= row["conversion_score"] < 0.70 and row["plan_rank"] == 0:
        return ActionCandidate(
            customer_id=row["customer_id"],
            action_type="enroll_nurture_sequence",
            reason=(
                f"Warm Free-tier user (conv={row['conversion_score']:.0%}). "
                f"Enrol in value-demonstration nurture sequence."
            ),
            priority=2,
            rule_id="CNV-002",
            triggered_by={
                "conversion_score": row["conversion_score"],
                "plan": row["plan"],
                "tenure_days": row["tenure_days"],
            },
        )


@rule("CNV-003", priority=3)
def starter_to_pro_trigger(row):
    """Starter customer hitting usage limits — Pro upsell moment."""
    if (
        row["plan_rank"] == 1
        and row["event_count_30d"] >= 20
        and row["conversion_score"] >= 0.50
    ):
        return ActionCandidate(
            customer_id=row["customer_id"],
            action_type="send_upgrade_offer",
            reason=(
                f"Starter customer with high usage ({int(row['event_count_30d'])} events/30d). "
                f"Likely hitting plan limits. Pro upgrade opportunity."
            ),
            priority=3,
            rule_id="CNV-003",
            triggered_by={
                "event_count_30d": row["event_count_30d"],
                "conversion_score": row["conversion_score"],
                "plan": row["plan"],
            },
            context={"suggested_plan": "Pro"},
        )


# ─── LOYALTY / HEALTH RULES ───────────────────────────────────────────────────

@rule("LYL-001", priority=2)
def champion_reward(row):
    """Champion-segment customers — send loyalty reward."""
    if row["segment"] == "Champion" and row["tenure_days"] >= 180:
        return ActionCandidate(
            customer_id=row["customer_id"],
            action_type="send_loyalty_reward",
            reason=(
                f"Champion customer with {int(row['tenure_days'])} days tenure. "
                f"Reward loyalty to reinforce retention."
            ),
            priority=2,
            rule_id="LYL-001",
            triggered_by={
                "segment": row["segment"],
                "tenure_days": row["tenure_days"],
                "health_score": row["health_score"],
            },
        )


@rule("LYL-002", priority=1)
def low_health_check_in(row):
    """Low health score but not yet churning — check-in email."""
    if row["health_score"] < 35 and row["churn_score"] < 0.45:
        return ActionCandidate(
            customer_id=row["customer_id"],
            action_type="send_health_checkin",
            reason=(
                f"Health score degraded to {row['health_score']:.0f}/100. "
                f"Churn risk still manageable. Schedule a check-in."
            ),
            priority=1,
            rule_id="LYL-002",
            triggered_by={
                "health_score": row["health_score"],
                "churn_score": row["churn_score"],
                "nps_score": row["nps_score"],
            },
        )


# ─── Engine ───────────────────────────────────────────────────────────────────

def run_rules(df: pd.DataFrame) -> list[ActionCandidate]:
    """
    Evaluate all rules against every customer row.
    Returns a flat list of ActionCandidates (one customer can generate multiple).
    """
    candidates: list[ActionCandidate] = []
    for _, row in df.iterrows():
        for rule_def in RULES:
            try:
                result = rule_def["fn"](row)
                if result is not None:
                    candidates.append(result)
            except Exception as e:
                print(f"[Rule {rule_def['id']}] Error on {row['customer_id']}: {e}")
    return candidates


def get_enriched_df(company_id: str | None = None) -> pd.DataFrame:
    """Merge feature matrix with ML predictions for rule evaluation."""
    cid     = company_id or get_tenant().company_id
    feat_df = build_customer_features(company_id=cid)
    pred_df = read_sql("SELECT * FROM ml_predictions WHERE company_id = ?", params=(cid,))

    df = feat_df.merge(
        pred_df[["customer_id", "churn_score", "conversion_score", "segment", "ltv_estimate"]],
        on="customer_id", how="left"
    )
    df["churn_score"]      = df["churn_score"].fillna(0.0)
    df["conversion_score"] = df["conversion_score"].fillna(0.0)
    df["segment"]          = df["segment"].fillna("Unknown")
    return df


def evaluate(company_id: str | None = None, verbose: bool = True) -> list[ActionCandidate]:
    """Full rule evaluation pass over all customers."""
    df         = get_enriched_df(company_id=company_id)
    candidates = run_rules(df)

    if verbose:
        from collections import Counter
        action_counts  = Counter(c.action_type for c in candidates)
        priority_counts = Counter(c.priority for c in candidates)
        rule_counts    = Counter(c.rule_id for c in candidates)

        print(f"\n── Rule Engine Results ───────────────────────────────")
        print(f"  Customers evaluated : {len(df)}")
        print(f"  Candidates generated: {len(candidates)}")
        print(f"\n  By action type:")
        for act, n in sorted(action_counts.items(), key=lambda x: -x[1]):
            print(f"    {act:35s}: {n}")
        print(f"\n  By priority:")
        for p in sorted(priority_counts.keys(), reverse=True):
            label = {5:"CRITICAL",4:"HIGH",3:"MEDIUM",2:"LOW",1:"INFO"}[p]
            print(f"    P{p} {label:10s}: {priority_counts[p]}")
        print(f"\n  By rule fired:")
        for r, n in sorted(rule_counts.items()):
            print(f"    {r}: {n}")
        print()

    return candidates


if __name__ == "__main__":
    candidates = evaluate()
    print("Sample candidates:")
    for c in sorted(candidates, key=lambda x: -x.priority)[:5]:
        print(f"  [{c.rule_id}] P{c.priority} {c.customer_id} → {c.action_type}")
        print(f"    {c.reason}")
