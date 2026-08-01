"""
ACIA — Action Executor
Pulls pending actions from the queue, builds personalised content,
simulates execution (email send / CSM task / support call), and
writes outcomes back to the DB as the feedback signal.
"""

from __future__ import annotations
import random
import time
import json
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from actions.templates import build_template, MessageTemplate
from actions.sendgrid_email import send_email, validate_config, DRY_RUN_MODE

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.db import get_connection
from utils.tenancy import get_tenant
random.seed()   # fresh seed per run for outcome simulation


# ── Outcome simulator ─────────────────────────────────────────────────────────
# Real systems would call SendGrid, Salesforce, etc.
# We simulate realistic outcome probabilities per action type.

OUTCOME_PROFILES: dict[str, dict] = {
    "send_retention_email": {
        "success_rate": 0.42,
        "outcomes": {
            "opened_clicked":   0.35,
            "opened_only":      0.25,
            "discount_redeemed":0.15,
            "unsubscribed":     0.05,
            "bounced":          0.03,
            "no_response":      0.17,
        },
    },
    "send_reengagement_email": {
        "success_rate": 0.31,
        "outcomes": {
            "logged_in":        0.28,
            "opened_only":      0.22,
            "no_response":      0.45,
            "unsubscribed":     0.05,
        },
    },
    "send_upgrade_offer": {
        "success_rate": 0.28,
        "outcomes": {
            "upgraded":         0.18,
            "clicked_not_converted": 0.22,
            "opened_only":      0.28,
            "no_response":      0.30,
            "unsubscribed":     0.02,
        },
    },
    "send_loyalty_reward": {
        "success_rate": 0.70,
        "outcomes": {
            "reward_claimed":   0.55,
            "opened_only":      0.25,
            "no_response":      0.18,
            "unsubscribed":     0.02,
        },
    },
    "send_health_checkin": {
        "success_rate": 0.55,
        "outcomes": {
            "replied_positive": 0.30,
            "replied_negative": 0.12,
            "opened_only":      0.28,
            "no_response":      0.30,
        },
    },
    "escalate_to_csm": {
        "success_rate": 0.65,
        "outcomes": {
            "call_completed_retained": 0.45,
            "call_completed_churned":  0.15,
            "no_answer":               0.25,
            "scheduled_followup":      0.15,
        },
    },
    "proactive_support_call": {
        "success_rate": 0.58,
        "outcomes": {
            "issue_resolved":   0.45,
            "escalated_csm":    0.08,
            "no_answer":        0.28,
            "callback_scheduled":0.19,
        },
    },
    "enroll_nurture_sequence": {
        "success_rate": 0.40,
        "outcomes": {
            "enrolled_active":  0.55,
            "enrolled_passive": 0.30,
            "unsubscribed":     0.08,
            "already_enrolled": 0.07,
        },
    },
}

def _simulate_outcome(action_type: str) -> tuple[str, str]:
    """Returns (outcome_label, success|failure|neutral)."""
    profile  = OUTCOME_PROFILES.get(action_type, {})
    outcomes = profile.get("outcomes", {"completed": 1.0})
    labels   = list(outcomes.keys())
    weights  = list(outcomes.values())
    chosen   = random.choices(labels, weights=weights, k=1)[0]

    positive = {"opened_clicked","discount_redeemed","logged_in","upgraded",
                "reward_claimed","replied_positive","call_completed_retained",
                "issue_resolved","enrolled_active","enrolled_passive","replied_negative"}
    negative = {"unsubscribed","bounced","call_completed_churned","no_response",
                "no_answer","already_enrolled"}

    if chosen in positive:
        status = "success"
    elif chosen in negative:
        status = "failure"
    else:
        status = "neutral"

    return chosen, status


# ── Execution result ──────────────────────────────────────────────────────────
@dataclass
class ExecutionResult:
    action_id:   str
    customer_id: str
    action_type: str
    template:    MessageTemplate
    outcome:     str
    status:      str       # success | failure | neutral
    executed_at: str


# ── Core executor ─────────────────────────────────────────────────────────────

def _load_context(customer_ids: list[str]) -> tuple[dict, dict]:
    """Fetch customer features + ML predictions for a batch of customers."""
    conn = get_connection()
    placeholders = ",".join("?" * len(customer_ids))

    cid = get_tenant().company_id
    cust_rows = conn.execute(
        f"SELECT * FROM customers WHERE customer_id IN ({placeholders}) AND company_id=?",
        customer_ids + [cid]
    ).fetchall()
    cust_cols = [d[0] for d in conn.execute("SELECT * FROM customers LIMIT 0").description]
    customers = {r[0]: dict(zip(cust_cols, r)) for r in cust_rows}

    pred_rows = conn.execute(
        f"SELECT * FROM ml_predictions WHERE customer_id IN ({placeholders})",
        customer_ids
    ).fetchall()
    pred_cols = [d[0] for d in conn.execute("SELECT * FROM ml_predictions LIMIT 0").description]
    predictions = {r[1]: dict(zip(pred_cols, r)) for r in pred_rows}

    # Also pull feature-derived fields we need for templates
    feat_rows = conn.execute(
        f"""SELECT c.customer_id,
               CAST(julianday('now') - julianday(MAX(e.event_date)) AS INTEGER) AS days_since_last_event,
               COUNT(t.ticket_id) AS open_tickets,
               AVG(t.sentiment_score) AS avg_sentiment,
               CAST(julianday('now') - julianday(c.signup_date) AS INTEGER) AS tenure_days
            FROM customers c
            LEFT JOIN engagement_events e ON e.customer_id = c.customer_id
            LEFT JOIN support_tickets t   ON t.customer_id = c.customer_id AND t.status = 'open'
            WHERE c.customer_id IN ({placeholders})
            GROUP BY c.customer_id""",
        customer_ids
    ).fetchall()
    for row in feat_rows:
        cid = row[0]
        if cid in customers:
            customers[cid]["days_since_last_event"] = row[1] or 0
            customers[cid]["open_tickets"]          = row[2] or 0
            customers[cid]["avg_sentiment"]         = row[3] or 0.0
            customers[cid]["tenure_days"]           = row[4] or 0

    conn.close()
    return customers, predictions


def execute_batch(
    limit:    int  = 50,
    dry_run:  bool = False,
    verbose:  bool = True,
) -> list[ExecutionResult]:
    """
    Pull up to `limit` pending actions, execute them, write outcomes to DB.
    dry_run=True: build templates and simulate but don't write to DB.
    """
    conn   = get_connection()
    rows   = conn.execute(
        "SELECT * FROM agent_actions WHERE status='pending' "
        "ORDER BY priority DESC, created_at ASC LIMIT ?",
        (limit,)
    ).fetchall()
    cols   = [d[0] for d in conn.execute("SELECT * FROM agent_actions LIMIT 0").description]
    actions = [dict(zip(cols, r)) for r in rows]
    conn.close()

    if not actions:
        if verbose:
            print("  No pending actions in queue.")
        return []

    # Batch-load customer context
    cust_ids = list({a["customer_id"] for a in actions})
    customers, predictions = _load_context(cust_ids)

    results: list[ExecutionResult] = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for action in actions:
        cid      = action["customer_id"]
        customer = customers.get(cid, {"customer_id": cid, "name": cid,
                                       "plan": "Unknown", "mrr": 0})
        pred     = predictions.get(cid, {})

        # Build personalised template
        template = build_template(action, customer, pred)

        # ── Execute action ────────────────────────────────────────────────────
        if template.channel == "email" and not dry_run:
            # Real email via SendGrid
            to_email = customer.get("email", f"{cid.lower()}@example.com")
            to_name  = customer.get("name", cid)

            email_result = send_email(
                to_email=to_email,
                to_name=to_name,
                subject=template.subject,
                body=template.body,
                action_type=action["action_type"],
                customer_id=cid,
            )

            if email_result.success:
                outcome_label  = "email_sent" if not email_result.dry_run else "dry_run_sent"
                outcome_status = "success"
            else:
                outcome_label  = "email_failed"
                outcome_status = "failure"

        else:
            # Non-email actions (CSM tasks, support calls) — simulate outcome
            outcome_label, outcome_status = _simulate_outcome(action["action_type"])

        result = ExecutionResult(
            action_id=action["action_id"],
            customer_id=cid,
            action_type=action["action_type"],
            template=template,
            outcome=outcome_label,
            status=outcome_status,
            executed_at=now,
        )
        results.append(result)

        # Write outcome to DB
        if not dry_run:
            conn = get_connection()
            conn.execute(
                "UPDATE agent_actions SET status='executed', outcome=?, executed_at=? "
                "WHERE action_id=?",
                (outcome_label, now, action["action_id"])
            )
            # Log to email_log if it's an email action
            if template.channel == "email":
                opened  = 1 if outcome_label in ("email_sent", "dry_run_sent") else 0
                clicked = 0  # real click tracking comes from SendGrid webhooks
                conn.execute(
                    "INSERT INTO email_log (email_id, customer_id, email_type, subject, opened, clicked, sent_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (f"ACIA-{action['action_id'][:8]}", cid, action["action_type"],
                     template.subject, opened, clicked, now)
                )
            conn.commit()
            conn.close()

    return results


# ── Outcome summary ───────────────────────────────────────────────────────────

def print_execution_report(results: list[ExecutionResult], verbose: bool = True):
    if not results:
        return

    total    = len(results)
    by_status = {"success": 0, "failure": 0, "neutral": 0}
    by_action: dict[str, dict] = {}
    email_results: list[ExecutionResult] = []

    for r in results:
        by_status[r.status] = by_status.get(r.status, 0) + 1
        at = r.action_type
        if at not in by_action:
            by_action[at] = {"total": 0, "success": 0}
        by_action[at]["total"]   += 1
        by_action[at]["success"] += 1 if r.status == "success" else 0
        if r.template.channel == "email":
            email_results.append(r)

    print(f"\n{'='*54}")
    print(f"  ACIA — Execution Report")
    print(f"{'='*54}")
    print(f"\n  Actions executed : {total}")
    print(f"  ✅  Success      : {by_status['success']}  ({by_status['success']/total:.0%})")
    print(f"  ➡️  Neutral      : {by_status['neutral']}  ({by_status['neutral']/total:.0%})")
    print(f"  ❌  Failure      : {by_status['failure']}  ({by_status['failure']/total:.0%})")

    print(f"\n  Success rate by action type:")
    for atype, stats in sorted(by_action.items(), key=lambda x: -x[1]["success"]):
        rate = stats["success"] / stats["total"]
        bar  = "█" * int(rate * 20)
        print(f"    {atype:35s}: {rate:.0%}  {bar}")

    if email_results and verbose:
        print(f"\n  Sample email generated:")
        sample = random.choice(email_results)
        t = sample.template
        print(f"  ┌─ To      : {sample.customer_id}")
        print(f"  │  Channel : {t.channel}")
        print(f"  │  Subject : {t.subject}")
        print(f"  │  Outcome : {sample.outcome} ({sample.status})")
        if t.discount_pct:
            print(f"  │  Discount: {t.discount_pct}% off")
        print(f"  └─ Body preview:")
        for line in t.body.strip().split("\n")[:8]:
            print(f"       {line}")
        print()

    print(f"{'='*54}\n")


if __name__ == "__main__":
    print("Running action executor on pending queue...\n")
    results = execute_batch(limit=100, dry_run=False, verbose=True)
    print_execution_report(results, verbose=True)
