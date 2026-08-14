"""
ACIA — Main Entry Point
Runs the full autonomous CRM pipeline end to end.

Usage:
  python main.py                  # full pipeline (no LLM)
  python main.py --llm            # enable Claude API planning
  python main.py --setup          # generate data + train models only
  python main.py --api            # start REST API server
  python main.py --cycle          # run one agent decision cycle
"""

from __future__ import annotations
import argparse
import sys
import time
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))


# ── Banner ────────────────────────────────────────────────────────────────────

BANNER = r"""
 █████╗  ██████╗██╗ █████╗
██╔══██╗██╔════╝██║██╔══██╗
███████║██║     ██║███████║
██╔══██║██║     ██║██╔══██║
██║  ██║╚██████╗██║██║  ██║
╚═╝  ╚═╝ ╚═════╝╚═╝╚═╝  ╚═╝
Autonomous CRM Intelligence Agent  v1.0
"""


# ── Pipeline stages ───────────────────────────────────────────────────────────

def stage(label: str):
    print(f"\n{'─'*54}")
    print(f"  {label}")
    print(f"{'─'*54}")


def run_setup(verbose: bool = True) -> None:
    """Generate synthetic data and train all ML models."""
    stage("Stage 1 of 4 — Data generation")
    from data.generate import build_database, print_summary
    customers, txns, events, tickets, emails = build_database()
    if verbose:
        print_summary(customers, txns, events, tickets, emails)

    stage("Stage 2 of 4 — ML model training + inference")
    from models.orchestrator import train_all, run_inference_and_store
    train_all(verbose=verbose)
    preds = run_inference_and_store(verbose=verbose)
    print(f"  ✅  {len(preds)} customers scored.")


def run_cycle(use_llm: bool = False, llm_budget: int = 20, verbose: bool = True, company_id: str = "default") -> dict:
    """Run one full agent decision + execution cycle."""
    stage("Stage 3 of 4 — Agent decision cycle")
    from agent.runner import run_agent_cycle
    summary = run_agent_cycle(use_llm=use_llm, llm_budget=llm_budget, verbose=verbose, company_id=company_id)

    stage("Stage 4 of 4 — Action execution + feedback")
    from actions.executor import execute_batch, print_execution_report
    from actions.feedback import apply_feedback

    results = execute_batch(limit=300, dry_run=False, verbose=False)
    if verbose:
        print_execution_report(results, verbose=True)

    fb = apply_feedback(lookback_hours=48, verbose=verbose)
    return {"cycle": summary, "executed": len(results), "feedback": fb}


def run_full_pipeline(use_llm: bool = False, llm_budget: int = 20) -> None:
    """Full end-to-end ACIA pipeline."""
    t0 = time.time()
    print(BANNER)
    print(f"  Started : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  LLM mode: {'enabled (Claude API)' if use_llm else 'disabled (rule passthrough)'}")

    run_setup(verbose=True)
    result = run_cycle(use_llm=use_llm, llm_budget=llm_budget, verbose=True)

    elapsed = round(time.time() - t0, 1)
    print(f"\n{'='*54}")
    print(f"  ✅  ACIA pipeline complete in {elapsed}s")
    print(f"  Actions queued   : {result['cycle'].get('queued_actions', 0)}")
    print(f"  Actions executed : {result['executed']}")
    print(f"  LLM decisions    : {result['cycle'].get('llm_enhanced', 0)}")
    print(f"{'='*54}\n")


def run_api(host: str = "0.0.0.0", port: int = 8000) -> None:
    """Start the FastAPI REST server."""
    try:
        import uvicorn
    except ImportError:
        print("❌  uvicorn not installed. Run:  pip install fastapi uvicorn")
        sys.exit(1)

    print(BANNER)
    print(f"  Starting REST API on http://{host}:{port}")
    print(f"  Interactive docs : http://localhost:{port}/docs")
    print(f"  ReDoc            : http://localhost:{port}/redoc\n")

    import uvicorn
    uvicorn.run("api:app", host=host, port=port, reload=True, app_dir=str(ROOT))


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="acia",
        description="Autonomous CRM Intelligence Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                  # full pipeline, no LLM
  python main.py --llm            # full pipeline with Claude API
  python main.py --setup          # data generation + model training only
  python main.py --cycle --llm    # one agent cycle with Claude API
  python main.py --api            # start REST API on port 8000
  python main.py --api --port 9000
        """,
    )
    p.add_argument("--setup",      action="store_true", help="Generate data and train models only")
    p.add_argument("--cycle",      action="store_true", help="Run one agent decision + execution cycle")
    p.add_argument("--api",        action="store_true", help="Start FastAPI REST server")
    p.add_argument("--llm",        action="store_true", help="Enable Claude API for LLM planning")
    p.add_argument("--llm-budget", type=int, default=20, metavar="N", help="Max LLM calls per cycle (default: 20)")
    p.add_argument("--host",       default="0.0.0.0",   help="API host (default: 0.0.0.0)")
    p.add_argument("--port",       type=int, default=8000, help="API port (default: 8000)")
    p.add_argument("--quiet",      action="store_true", help="Suppress verbose output")
    p.add_argument("--company-id",  default="default",   help="Company/tenant ID to run cycle on")
    return p


def main() -> None:
    parser = build_parser()
    args   = parser.parse_args()
    verbose = not args.quiet

    # Check API key if LLM mode requested
    if args.llm and not os.environ.get("ANTHROPIC_API_KEY"):
        print("⚠️  Warning: ANTHROPIC_API_KEY not set. LLM planning will fall back to rule passthrough.")
        print("   Set it with:  export ANTHROPIC_API_KEY=sk-ant-...")

    if args.api:
        run_api(host=args.host, port=args.port)
    elif args.setup:
        print(BANNER)
        run_setup(verbose=verbose)
        print("\n✅  Setup complete. Run `python main.py --cycle` to trigger an agent cycle.\n")
    elif args.cycle:
        if not (ROOT / "data" / "acia.db").exists():
            print("⚠️  Database not found. Running setup first...")
            run_setup(verbose=False)
        print(BANNER)
        company_id = args.company_id.replace("-", "_") if hasattr(args, "company_id") else "default"
        run_cycle(use_llm=args.llm, llm_budget=args.llm_budget, verbose=verbose, company_id=getattr(args, "company_id", "default"))
    else:
        run_full_pipeline(use_llm=args.llm, llm_budget=args.llm_budget)


import os
if __name__ == "__main__":
    main()
