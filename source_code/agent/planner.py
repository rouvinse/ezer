"""
ACIA — LLM Planner (Claude API)
Takes ActionCandidates from the rule engine and uses Claude to:
  1. Resolve conflicts when multiple rules fire for one customer
  2. Personalise action reasoning with full customer context
  3. Decide on ambiguous edge cases rules can't handle
  4. Generate the final action plan with human-readable justification
"""

from __future__ import annotations
import json
import sqlite3
import time
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, asdict
from collections import defaultdict

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from agent.rules import ActionCandidate

DB_PATH = Path(__file__).parent.parent / "data" / "acia.db"


# ── Planned Action (output of planner) ───────────────────────────────────────
@dataclass
class PlannedAction:
    customer_id:   str
    action_type:   str
    reason:        str          # LLM-enriched justification
    priority:      int
    rule_id:       str
    llm_enhanced:  bool         # True if Claude revised the candidate
    confidence:    float        # 0–1, LLM self-reported confidence
    metadata:      dict


# ── Prompts ────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are ACIA, an autonomous CRM intelligence agent.
Your role is to review customer data and action candidates, then decide the optimal action plan.

You receive:
- Customer profile data (plan, MRR, tenure, health score, segment, churn/conversion scores)
- One or more action candidates triggered by business rules

You must output a single JSON object with this exact structure:
{
  "action_type": "<chosen action type>",
  "reason": "<clear 1-2 sentence justification referencing the customer data>",
  "priority": <integer 1-5>,
  "confidence": <float 0.0-1.0>,
  "notes": "<any important considerations>"
}

Action types available:
- escalate_to_csm          (assign to customer success manager)
- send_retention_email     (personalised retention campaign)
- send_reengagement_email  (re-activate dormant users)
- send_upgrade_offer       (upsell to higher plan)
- enroll_nurture_sequence  (drip campaign for prospects)
- proactive_support_call   (schedule outbound call)
- send_loyalty_reward      (reward high-value loyal customers)
- send_health_checkin      (low-pressure check-in for at-risk)
- no_action                (customer is healthy, no intervention needed)

Rules:
- If multiple actions conflict, choose the highest-impact one
- Always consider LTV: high-value customers at risk deserve higher priority
- Never escalate to CSM for low-MRR free-tier customers — use email instead
- For new customers (tenure < 30 days), prefer nurture over retention
- Output ONLY valid JSON, no preamble or explanation outside the JSON block
"""

def _build_customer_prompt(candidates: list[ActionCandidate], customer_row: dict) -> str:
    c = customer_row
    candidate_list = "\n".join([
        f"  - Rule {cand.rule_id} → {cand.action_type} (P{cand.priority}): {cand.reason}"
        for cand in candidates
    ])
    return f"""Customer: {c.get('customer_id')}
Plan: {c.get('plan')} | MRR: ${c.get('mrr', 0):.2f}/mo | Tenure: {int(c.get('tenure_days', 0))} days
Segment: {c.get('segment')} | Health: {c.get('health_score', 0):.0f}/100 | NPS: {c.get('nps_score', 0)}
Churn score: {c.get('churn_score', 0):.3f} | Conversion score: {c.get('conversion_score', 0):.3f}
LTV estimate: ${c.get('ltv_estimate', 0):.0f}
Events (30d): {int(c.get('event_count_30d', 0))} | Tickets open: {int(c.get('open_tickets', 0))} | Sentiment: {c.get('avg_sentiment', 0):.2f}
Days since last event: {int(c.get('days_since_last_event', 0))}

Action candidates fired:
{candidate_list}

Decide the single best action for this customer."""


# ── Claude API call ────────────────────────────────────────────────────────────

def _call_claude(prompt: str, retries: int = 2) -> dict | None:
    """Call Claude API and parse JSON response."""
    try:
        import urllib.request
        payload = json.dumps({
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1000,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": prompt}]
        }).encode()

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())

        text = "".join(
            block.get("text", "") for block in data.get("content", [])
            if block.get("type") == "text"
        )
        # Strip markdown fences if present
        text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(text)

    except Exception as e:
        if retries > 0:
            time.sleep(1)
            return _call_claude(prompt, retries - 1)
        print(f"  [LLM] API call failed: {e}")
        return None


# ── Planner logic ──────────────────────────────────────────────────────────────

def _passthrough_plan(candidates: list[ActionCandidate]) -> PlannedAction:
    """Fallback: pick highest-priority candidate without LLM."""
    best = max(candidates, key=lambda c: c.priority)
    return PlannedAction(
        customer_id=best.customer_id,
        action_type=best.action_type,
        reason=best.reason,
        priority=best.priority,
        rule_id=best.rule_id,
        llm_enhanced=False,
        confidence=0.7,
        metadata={"triggered_by": best.triggered_by, "context": best.context},
    )


def plan_actions(
    candidates: list[ActionCandidate],
    customer_lookup: dict,
    use_llm: bool = True,
    llm_budget: int = 30,       # max customers to send to LLM (cost control)
    verbose: bool = True,
) -> list[PlannedAction]:
    """
    Groups candidates by customer, resolves conflicts, and produces
    one PlannedAction per customer.

    use_llm=True  → complex/conflicted cases go to Claude
    use_llm=False → rule-based passthrough only (for testing / cost control)
    llm_budget    → caps LLM calls; remaining are handled by passthrough
    """
    # Group candidates by customer
    by_customer: dict[str, list[ActionCandidate]] = defaultdict(list)
    for c in candidates:
        by_customer[c.customer_id].append(c)

    plans: list[PlannedAction] = []
    llm_calls = 0

    # Customers that benefit most from LLM: multi-rule conflict OR high LTV
    def needs_llm(cust_id: str, cands: list[ActionCandidate]) -> bool:
        if not use_llm:
            return False
        if llm_calls >= llm_budget:
            return False
        row = customer_lookup.get(cust_id, {})
        has_conflict = len(set(c.action_type for c in cands)) > 1
        high_ltv     = float(row.get("ltv_estimate", 0)) >= 500
        high_risk    = float(row.get("churn_score", 0)) >= 0.60
        return has_conflict or high_ltv or high_risk

    for cust_id, cands in by_customer.items():
        row = customer_lookup.get(cust_id, {})

        if needs_llm(cust_id, cands):
            prompt   = _build_customer_prompt(cands, row)
            llm_resp = _call_claude(prompt)
            llm_calls += 1

            if llm_resp:
                # Merge rule context with LLM decision
                best_rule = max(cands, key=lambda c: c.priority)
                plan = PlannedAction(
                    customer_id=cust_id,
                    action_type=llm_resp.get("action_type", best_rule.action_type),
                    reason=llm_resp.get("reason", best_rule.reason),
                    priority=int(llm_resp.get("priority", best_rule.priority)),
                    rule_id=best_rule.rule_id,
                    llm_enhanced=True,
                    confidence=float(llm_resp.get("confidence", 0.8)),
                    metadata={
                        "llm_notes": llm_resp.get("notes", ""),
                        "candidates": len(cands),
                        "triggered_by": best_rule.triggered_by,
                    },
                )
                plans.append(plan)
                continue

        # Passthrough (single rule, low LTV, or LLM budget exhausted)
        plans.append(_passthrough_plan(cands))

    if verbose:
        llm_count   = sum(1 for p in plans if p.llm_enhanced)
        rule_count  = len(plans) - llm_count
        action_dist = {}
        for p in plans:
            action_dist[p.action_type] = action_dist.get(p.action_type, 0) + 1

        print(f"\n── LLM Planner Results ───────────────────────────────")
        print(f"  Customers with actions : {len(plans)}")
        print(f"  LLM-enhanced decisions : {llm_count}")
        print(f"  Rule passthrough       : {rule_count}")
        print(f"\n  Final action plan:")
        for act, n in sorted(action_dist.items(), key=lambda x: -x[1]):
            print(f"    {act:35s}: {n}")
        print()

    return plans


if __name__ == "__main__":
    import pandas as pd
    from agent.rules import evaluate, get_enriched_df

    df         = get_enriched_df()
    candidates = evaluate(verbose=False)
    cust_lookup = df.set_index("customer_id").to_dict("index")

    print(f"Running LLM planner on {len(candidates)} candidates...")
    plans = plan_actions(candidates, cust_lookup, use_llm=True, llm_budget=10, verbose=True)

    print("Sample planned actions (LLM-enhanced):")
    for p in [x for x in plans if x.llm_enhanced][:3]:
        print(f"\n  {p.customer_id} → {p.action_type} [P{p.priority}, conf={p.confidence:.2f}]")
        print(f"  {p.reason}")
        if p.metadata.get("llm_notes"):
            print(f"  Notes: {p.metadata['llm_notes']}")
