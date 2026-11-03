"""
ACIA — Action Scheduler
Takes PlannedActions from the planner, applies deduplication,
cooldown logic, and priority scoring, then writes the final
action queue to the agent_actions table.

SEC-NEW-002 FIX: _get_recent_actions() now accepts and applies company_id
so cooldown checks are scoped per-tenant. A cooldown triggered by
Company A's actions no longer blocks Company B's customers.
"""

from __future__ import annotations
import uuid
import json
from pathlib import Path
from datetime import datetime, timedelta
from dataclasses import asdict

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from agent.planner import PlannedAction

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.db import get_connection
from utils.tenancy import get_tenant
from actions.approval import needs_approval, queue_for_approval, ensure_approval_schema

# Cooldown: don't re-queue the same action_type for the same customer
# within this many days
COOLDOWN_DAYS: dict[str, int] = {
    "escalate_to_csm":          3,
    "send_retention_email":     7,
    "send_reengagement_email":  14,
    "send_upgrade_offer":       10,
    "enroll_nurture_sequence":  30,
    "proactive_support_call":   5,
    "send_loyalty_reward":      90,
    "send_health_checkin":      14,
    "no_action":                0,
}


def _get_recent_actions(conn, company_id: str, days_back: int = 90) -> set[tuple]:
    """
    Returns set of (customer_id, action_type) executed within cooldown window
    for the given tenant.

    SEC-NEW-002: company_id is now required so cooldown checks are isolated
    per tenant. Previously this queried all tenants' executed actions, meaning
    a high-volume tenant could accidentally suppress actions for another.
    """
    cutoff = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT customer_id, action_type, executed_at FROM agent_actions "
        "WHERE status='executed' AND executed_at >= ? AND company_id=?",
        (cutoff, company_id)
    ).fetchall()

    # Build set of blocked (customer_id, action_type) pairs still in cooldown
    blocked = set()
    for cust_id, action_type, executed_at in rows:
        cooldown = COOLDOWN_DAYS.get(action_type, 7)
        if executed_at:
            exec_dt  = datetime.strptime(executed_at, "%Y-%m-%d %H:%M:%S")
            if (datetime.now() - exec_dt).days < cooldown:
                blocked.add((cust_id, action_type))
    return blocked


def _score_action(plan: PlannedAction, ltv: float) -> float:
    """
    Composite urgency score for queue ordering.
    Higher = execute sooner.
    """
    ltv_factor = min((ltv or 0) / 2000, 1.0)   # normalise LTV up to $2k
    return (
        plan.priority      * 3.0 +
        plan.confidence    * 1.5 +
        ltv_factor         * 2.0
    )


def schedule(
    plans:      list[PlannedAction],
    ltv_lookup: dict[str, float],
    company_id: str | None = None,
    verbose:    bool = True,
) -> list[dict]:
    """
    1. Filter out cooldown-blocked actions (per-tenant)
    2. Score and sort by urgency
    3. Write to agent_actions table (status='pending')
    Returns the list of queued action dicts.
    """
    cid_scope = company_id or get_tenant().company_id
    conn      = get_connection()

    # Cooldown check now scoped to this tenant only
    blocked = _get_recent_actions(conn, cid_scope)
    now     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    queued   = []
    skipped  = []

    for plan in plans:
        key = (plan.customer_id, plan.action_type)
        if key in blocked:
            skipped.append(plan)
            continue

        ltv   = ltv_lookup.get(plan.customer_id, 0.0)
        score = _score_action(plan, ltv)

        action = {
            "action_id":       str(uuid.uuid4()),
            "customer_id":     plan.customer_id,
            "action_type":     plan.action_type,
            "reason":          plan.reason,
            "priority":        plan.priority,
            "status":          "pending",
            "outcome":         None,
            "created_at":      now,
            "executed_at":     None,
            "company_id":      cid_scope,
            "_score":          score,           # internal, not stored
            "_ltv":            ltv,
            "_llm_enhanced":   plan.llm_enhanced,
            "_metadata":       json.dumps(plan.metadata),
        }
        queued.append(action)

    # Sort by urgency score descending
    queued.sort(key=lambda x: x["_score"], reverse=True)

    # Ensure approval schema exists
    ensure_approval_schema()

    # Write to DB (clear old pending first — scoped to tenant)
    conn.execute(
        "DELETE FROM agent_actions "
        "WHERE status IN ('pending','pending_approval') AND company_id=?",
        (cid_scope,)
    )
    for a in queued:
        requires_approval = needs_approval(a["priority"], cid_scope)
        initial_status    = "pending_approval" if requires_approval else "pending"

        conn.execute(
            "INSERT INTO agent_actions "
            "(action_id, customer_id, action_type, reason, priority, status, "
            " outcome, created_at, executed_at, company_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                a["action_id"], a["customer_id"], a["action_type"],
                a["reason"],    a["priority"],    initial_status,
                a["outcome"],   a["created_at"],  a["executed_at"],
                cid_scope,
            )
        )
    conn.commit()
    conn.close()

    if verbose:
        p_counts = {}
        for a in queued:
            p_counts[a["priority"]] = p_counts.get(a["priority"], 0) + 1

        print(f"\n── Scheduler Results ────────────────────────────────")
        print(f"  Plans received   : {len(plans)}")
        print(f"  Cooldown skipped : {len(skipped)}")
        print(f"  Actions queued   : {len(queued)}")
        print(f"\n  Priority breakdown:")
        label_map = {5:"CRITICAL",4:"HIGH",3:"MEDIUM",2:"LOW",1:"INFO"}
        for p in sorted(p_counts.keys(), reverse=True):
            print(f"    P{p} {label_map[p]:10s}: {p_counts[p]}")
        print(f"\n  Top 10 by urgency score:")
        for a in queued[:10]:
            llm_tag = "[LLM]" if a["_llm_enhanced"] else "     "
            print(f"    {llm_tag} P{a['priority']} {a['customer_id']} → {a['action_type']}"
                  f"  (score={a['_score']:.1f}, LTV=${a['_ltv']:.0f})")
        print()

    return queued


def get_pending_queue(limit: int = 100, company_id: str | None = None) -> list[dict]:
    """Fetch pending actions from DB, ordered by priority."""
    cid  = company_id or get_tenant().company_id
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM agent_actions WHERE status='pending' AND company_id=? "
        "ORDER BY priority DESC, created_at ASC LIMIT ?",
        (cid, limit)
    ).fetchall()
    cols = [d[0] for d in conn.execute("SELECT * FROM agent_actions LIMIT 0").description]
    conn.close()
    return [dict(zip(cols, row)) for row in rows]


if __name__ == "__main__":
    import pandas as pd
    from agent.rules    import evaluate, get_enriched_df
    from agent.planner  import plan_actions

    df          = get_enriched_df()
    candidates  = evaluate(verbose=False)
    ltv_lookup  = df.set_index("customer_id")["ltv_estimate"].to_dict()
    cust_lookup = df.set_index("customer_id").to_dict("index")

    plans = plan_actions(candidates, cust_lookup, use_llm=False, verbose=False)

    queue = schedule(plans, ltv_lookup, verbose=True)
    print(f"\nPending queue size: {len(get_pending_queue())}")
