"""
ACIA — Human Approval Queue
Allows humans to review, approve, edit, or reject agent actions
before they are executed. Bridges autonomous AI decisions with
human oversight — the trust layer for new customers.

Flow:
  1. Agent decision engine queues actions as status='pending_approval'
  2. Human logs into dashboard and sees the approval queue
  3. Human reviews each action: approve / reject / edit
  4. Approved actions move to status='pending' → executor picks them up
  5. Rejected actions move to status='rejected' with a reason
  6. Edited actions have their content updated before approval

Trust modes (configurable per company):
  - 'full_review'   → every action needs human approval (default for new tenants)
  - 'high_risk_only'→ only P4/P5 actions need approval
  - 'autonomous'    → no approval needed (for trusted, established tenants)
"""

from __future__ import annotations
import uuid
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass
from typing import Optional

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.db import get_connection, read_sql
from utils.tenancy import get_tenant

# ── Trust modes ───────────────────────────────────────────────────────────────
TRUST_MODE_FULL      = "full_review"     # all actions need approval
TRUST_MODE_HIGH_RISK = "high_risk_only"  # P4/P5 only
TRUST_MODE_AUTO      = "autonomous"      # no approval needed

# Priority threshold for high_risk_only mode
HIGH_RISK_PRIORITY   = 4


# ── Approval schema ────────────────────────────────────────────────────────────

APPROVAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS approval_settings (
    company_id      TEXT PRIMARY KEY,
    trust_mode      TEXT DEFAULT 'full_review',
    updated_at      TEXT,
    updated_by      TEXT
);

CREATE TABLE IF NOT EXISTS approval_log (
    log_id          TEXT PRIMARY KEY,
    action_id       TEXT NOT NULL,
    company_id      TEXT NOT NULL,
    decision        TEXT NOT NULL,
    decided_by      TEXT,
    original_reason TEXT,
    edited_reason   TEXT,
    rejection_note  TEXT,
    decided_at      TEXT NOT NULL,
    FOREIGN KEY(action_id) REFERENCES agent_actions(action_id)
);
"""


def ensure_approval_schema():
    """Create approval tables if they don't exist."""
    conn = get_connection()
    conn.executescript(APPROVAL_SCHEMA)
    conn.commit()
    conn.close()


# ── Trust mode management ─────────────────────────────────────────────────────

def get_trust_mode(company_id: str | None = None) -> str:
    """Get the trust mode for a company. Defaults to full_review."""
    cid = company_id or get_tenant().company_id
    try:
        df = read_sql(
            "SELECT trust_mode FROM approval_settings WHERE company_id = ?",
            params=(cid,)
        )
        if not df.empty:
            return str(df.iloc[0]["trust_mode"])
    except Exception:
        pass
    return TRUST_MODE_FULL


def set_trust_mode(trust_mode: str, company_id: str | None = None, updated_by: str = "admin"):
    """Set the trust mode for a company."""
    if trust_mode not in [TRUST_MODE_FULL, TRUST_MODE_HIGH_RISK, TRUST_MODE_AUTO]:
        raise ValueError(f"Invalid trust mode: {trust_mode}. Must be one of: full_review, high_risk_only, autonomous")

    cid = company_id or get_tenant().company_id
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    ensure_approval_schema()
    conn = get_connection()

    # Upsert
    existing = conn.execute(
        "SELECT company_id FROM approval_settings WHERE company_id = ?", (cid,)
    ).fetchone()

    if existing:
        conn.execute(
            "UPDATE approval_settings SET trust_mode=?, updated_at=?, updated_by=? WHERE company_id=?",
            (trust_mode, now, updated_by, cid)
        )
    else:
        conn.execute(
            "INSERT INTO approval_settings (company_id, trust_mode, updated_at, updated_by) VALUES (?,?,?,?)",
            (cid, trust_mode, now, updated_by)
        )

    conn.commit()
    conn.close()


def needs_approval(priority: int, company_id: str | None = None) -> bool:
    """
    Check if an action of this priority needs human approval.
    Based on the company's current trust mode.
    """
    mode = get_trust_mode(company_id)

    if mode == TRUST_MODE_AUTO:
        return False
    if mode == TRUST_MODE_HIGH_RISK:
        return priority >= HIGH_RISK_PRIORITY
    return True  # full_review — everything needs approval


# ── Queue management ──────────────────────────────────────────────────────────

def queue_for_approval(action_id: str, company_id: str | None = None):
    """
    Move an action to pending_approval status.
    Called by the scheduler when trust mode requires approval.
    """
    cid = company_id or get_tenant().company_id
    conn = get_connection()
    conn.execute(
        "UPDATE agent_actions SET status='pending_approval' "
        "WHERE action_id=? AND company_id=?",
        (action_id, cid)
    )
    conn.commit()
    conn.close()


def get_approval_queue(
    company_id: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """
    Fetch all actions waiting for human approval.
    Returns enriched list with customer context.
    """
    cid = company_id or get_tenant().company_id

    try:
        df = read_sql(
            """
            SELECT
                a.action_id,
                a.customer_id,
                a.action_type,
                a.reason,
                a.priority,
                a.status,
                a.created_at,
                c.name        AS customer_name,
                c.plan        AS customer_plan,
                c.mrr         AS customer_mrr,
                p.churn_score,
                p.conversion_score,
                p.segment,
                p.ltv_estimate
            FROM agent_actions a
            LEFT JOIN customers c       ON c.customer_id = a.customer_id AND c.company_id = a.company_id
            LEFT JOIN ml_predictions p  ON p.customer_id = a.customer_id AND p.company_id = a.company_id
            WHERE a.status = 'pending_approval' AND a.company_id = ?
            ORDER BY a.priority DESC, a.created_at ASC
            LIMIT ?
            """,
            params=(cid, limit)
        )
        return df.to_dict("records")
    except Exception as e:
        print(f"Error fetching approval queue: {e}")
        return []


def get_approval_stats(company_id: str | None = None) -> dict:
    """Summary counts for the approval queue dashboard."""
    cid = company_id or get_tenant().company_id
    conn = get_connection()

    pending = conn.execute(
        "SELECT COUNT(*) FROM agent_actions WHERE status='pending_approval' AND company_id=?",
        (cid,)
    ).fetchone()[0]

    by_priority = conn.execute(
        "SELECT priority, COUNT(*) FROM agent_actions "
        "WHERE status='pending_approval' AND company_id=? GROUP BY priority ORDER BY priority DESC",
        (cid,)
    ).fetchall()

    approved_today = conn.execute(
        "SELECT COUNT(*) FROM approval_log "
        "WHERE company_id=? AND decision='approved' AND DATE(decided_at)=DATE('now')",
        (cid,)
    ).fetchone()[0]

    rejected_today = conn.execute(
        "SELECT COUNT(*) FROM approval_log "
        "WHERE company_id=? AND decision='rejected' AND DATE(decided_at)=DATE('now')",
        (cid,)
    ).fetchone()[0]

    conn.close()

    priority_labels = {5: "Critical", 4: "High", 3: "Medium", 2: "Low", 1: "Info"}
    return {
        "pending_approval": pending,
        "by_priority": {priority_labels.get(p, str(p)): n for p, n in by_priority},
        "approved_today": approved_today,
        "rejected_today": rejected_today,
        "trust_mode": get_trust_mode(cid),
    }


# ── Decision handlers ─────────────────────────────────────────────────────────

def approve_action(
    action_id:     str,
    decided_by:    str = "human",
    edited_reason: str | None = None,
    company_id:    str | None = None,
) -> dict:
    """
    Approve an action — moves it to 'pending' so executor picks it up.
    Optionally override the reason text before executing.
    """
    cid = company_id or get_tenant().company_id
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ensure_approval_schema()

    conn = get_connection()

    # Fetch current reason for log
    row = conn.execute(
        "SELECT reason FROM agent_actions WHERE action_id=? AND company_id=?",
        (action_id, cid)
    ).fetchone()

    if not row:
        conn.close()
        return {"success": False, "error": "Action not found"}

    original_reason = row[0] if row else ""

    # Update action: approved → pending (ready for executor)
    if edited_reason:
        conn.execute(
            "UPDATE agent_actions SET status='pending', reason=? WHERE action_id=? AND company_id=?",
            (edited_reason, action_id, cid)
        )
    else:
        conn.execute(
            "UPDATE agent_actions SET status='pending' WHERE action_id=? AND company_id=?",
            (action_id, cid)
        )

    # Log the decision
    conn.execute(
        "INSERT INTO approval_log "
        "(log_id, action_id, company_id, decision, decided_by, original_reason, edited_reason, decided_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), action_id, cid, "approved",
         decided_by, original_reason, edited_reason, now)
    )

    conn.commit()
    conn.close()

    return {
        "success":      True,
        "action_id":    action_id,
        "decision":     "approved",
        "decided_by":   decided_by,
        "decided_at":   now,
        "reason_edited": bool(edited_reason),
    }


def reject_action(
    action_id:       str,
    rejection_note:  str = "",
    decided_by:      str = "human",
    company_id:      str | None = None,
) -> dict:
    """
    Reject an action — moves it to 'rejected' and logs the reason.
    Rejected actions are never executed.
    """
    cid = company_id or get_tenant().company_id
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ensure_approval_schema()

    conn = get_connection()

    row = conn.execute(
        "SELECT reason FROM agent_actions WHERE action_id=? AND company_id=?",
        (action_id, cid)
    ).fetchone()

    if not row:
        conn.close()
        return {"success": False, "error": "Action not found"}

    conn.execute(
        "UPDATE agent_actions SET status='rejected', outcome=? WHERE action_id=? AND company_id=?",
        (f"rejected: {rejection_note}" if rejection_note else "rejected", action_id, cid)
    )

    conn.execute(
        "INSERT INTO approval_log "
        "(log_id, action_id, company_id, decision, decided_by, original_reason, rejection_note, decided_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), action_id, cid, "rejected",
         decided_by, row[0], rejection_note, now)
    )

    conn.commit()
    conn.close()

    return {
        "success":        True,
        "action_id":      action_id,
        "decision":       "rejected",
        "rejection_note": rejection_note,
        "decided_by":     decided_by,
        "decided_at":     now,
    }


def bulk_approve(
    action_ids:  list[str],
    decided_by:  str = "human",
    company_id:  str | None = None,
) -> dict:
    """Approve multiple actions at once."""
    results = {"approved": [], "failed": []}
    for aid in action_ids:
        result = approve_action(aid, decided_by=decided_by, company_id=company_id)
        if result["success"]:
            results["approved"].append(aid)
        else:
            results["failed"].append(aid)
    return results


def approve_all_pending(
    company_id: str | None = None,
    decided_by: str = "human",
) -> dict:
    """Approve all currently pending_approval actions for a company."""
    queue = get_approval_queue(company_id=company_id, limit=500)
    action_ids = [a["action_id"] for a in queue]
    if not action_ids:
        return {"approved": [], "failed": [], "message": "No pending actions"}
    return bulk_approve(action_ids, decided_by=decided_by, company_id=company_id)


def get_approval_history(
    company_id: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Fetch recent approval decisions for audit trail."""
    cid = company_id or get_tenant().company_id
    try:
        df = read_sql(
            """
            SELECT l.*, a.action_type, a.priority
            FROM approval_log l
            LEFT JOIN agent_actions a ON a.action_id = l.action_id
            WHERE l.company_id = ?
            ORDER BY l.decided_at DESC
            LIMIT ?
            """,
            params=(cid, limit)
        )
        return df.to_dict("records")
    except Exception:
        return []
