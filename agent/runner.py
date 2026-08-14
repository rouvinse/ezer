"""
ACIA — Agent Runner
Orchestrates the full decision engine pipeline:
  Data → Rules → LLM Planner → Scheduler → DB Queue

Run this to trigger one full agent cycle.
"""

from __future__ import annotations
import pandas as pd
from pathlib import Path
from datetime import datetime

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.rules     import evaluate, get_enriched_df
from agent.planner   import plan_actions
from agent.scheduler import schedule, get_pending_queue

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.db import get_connection
from utils.tenancy import get_tenant, tenant_context


def run_agent_cycle(
    use_llm:    bool = True,
    llm_budget: int  = 20,
    company_id: str | None = None,
    verbose:    bool = True,
    dry_run:    bool = False,
) -> dict:
    """
    Full agent decision cycle. Returns summary stats dict.
    Scoped to the current tenant or company_id if provided.
    dry_run=True: previews actions without writing to DB (safe for demos).
    """
    start = datetime.now()
    cid   = company_id or get_tenant().company_id

    if verbose:
        print("=" * 54)
        print("  ACIA — Agent Decision Cycle")
        print(f"  Tenant  : {cid[:8]}...")
        print(f"  Started : {start.strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 54)

    # Step 1: Load enriched customer data (tenant-scoped)
    df = get_enriched_df(company_id=cid)
    if verbose:
        print(f"\n[1/3] Rule engine evaluating {len(df)} customers...")

    # Step 2: Run rule engine
    candidates = evaluate(company_id=cid, verbose=verbose)

    # Step 3: LLM planner resolves conflicts + enriches reasoning
    cust_lookup = df.set_index("customer_id").to_dict("index")
    ltv_lookup  = df.set_index("customer_id")["ltv_estimate"].to_dict()

    if verbose:
        print(f"[2/3] LLM Planner processing {len(candidates)} candidates "
              f"(LLM budget: {llm_budget} calls)...")

    plans = plan_actions(
        candidates, cust_lookup,
        use_llm=use_llm, llm_budget=llm_budget, verbose=verbose
    )

    # Step 4: Schedule and write to DB
    if verbose:
        print(f"[3/3] Scheduling {len(plans)} plans...")

    if dry_run:
        queue = [{"action_id": f"preview_{i}", "customer_id": p.customer_id, "action_type": p.action_type, "priority": p.priority, "reason": p.reason} for i, p in enumerate(plans)]
    else:
        queue = schedule(plans, ltv_lookup, company_id=cid, verbose=verbose)

    elapsed = (datetime.now() - start).total_seconds()

    summary = {
        "cycle_at":       start.isoformat(),
        "customers":      len(df),
        "candidates":     len(candidates),
        "plans":          len(plans),
        "queued_actions": len(queue),
        "llm_enhanced":   sum(1 for p in plans if p.llm_enhanced),
        "elapsed_sec":    round(elapsed, 1),
        "critical":       sum(1 for a in queue if a["priority"] == 5),
        "high":           sum(1 for a in queue if a["priority"] == 4),
    }

    if verbose:
        print(f"\n{'='*54}")
        print(f"  Cycle complete in {elapsed:.1f}s")
        print(f"  {summary['queued_actions']} actions queued  "
              f"({summary['critical']} critical, {summary['high']} high)")
        print(f"  {summary['llm_enhanced']} LLM-enhanced decisions")
        print(f"{'='*54}\n")

    return summary


if __name__ == "__main__":
    summary = run_agent_cycle(use_llm=True, llm_budget=20)
    print("\nFull pending queue (top 20):")
    queue = get_pending_queue(limit=20)
    for a in queue:
        print(f"  P{a['priority']} | {a['customer_id']} | {a['action_type']}")
        print(f"       {a['reason'][:90]}...")
