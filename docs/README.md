# Ezer — Autonomous CRM Intelligence Agent

An end-to-end agentic AI system that analyses customer data, predicts churn and conversion, and autonomously decides and executes engagement actions — all in a single Python project.

---

## What it does

Ezer simulates a real-world CRM intelligence layer with four autonomous capabilities:

| Capability | Description |
|---|---|
| **Predict** | Scores every customer for churn risk, conversion probability, and RFM segment |
| **Decide** | A rule engine + LLM planner (Claude API) determines the optimal action per customer |
| **Execute** | Sends retention emails, upsell offers, CSM escalations, and support tasks automatically |
| **Learn** | Outcome feedback updates health scores and flags customers for re-scoring |

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  DATA LAYER                                             │
│  customers · transactions · events · tickets · emails   │
└────────────────────────┬────────────────────────────────┘
                         │ 28 engineered features
┌────────────────────────▼────────────────────────────────┐
│  INTELLIGENCE LAYER (ML models)                         │
│  Churn predictor │ Conversion scorer │ Segment classifier│
└────────────────────────┬────────────────────────────────┘
                         │ scores per customer
┌────────────────────────▼────────────────────────────────┐
│  DECISION ENGINE (agentic core)                         │
│  Rule engine  →  LLM planner (Claude)  →  Priority queue│
└────────────────────────┬────────────────────────────────┘
                         │ ranked action queue
┌────────────────────────▼────────────────────────────────┐
│  ACTION LAYER                                           │
│  Retention emails · Upsell offers · CSM escalation      │
│  Support calls · Nurture sequences · Loyalty rewards    │
└────────────────────────┬────────────────────────────────┘
                         │ outcomes
                         └──────────────── feedback loop ──▶
```

---

## Project structure

```
acia/
├── main.py               ← single entry point (run this)
├── api.py                ← FastAPI REST layer
├── requirements.txt
│
├── data/
│   ├── generate.py       ← synthetic CRM data + SQLite DB
│   ├── features.py       ← feature engineering (28 features)
│   └── acia.db           ← generated SQLite database
│
├── models/
│   ├── churn.py          ← churn prediction (Random Forest)
│   ├── conversion.py     ← conversion scoring (Logistic Regression)
│   ├── segmentation.py   ← RFM + KMeans clustering
│   ├── orchestrator.py   ← trains all models + writes predictions to DB
│   └── artifacts/        ← saved .pkl model files
│
├── agent/
│   ├── rules.py          ← 8 business rules across churn/conversion/loyalty
│   ├── planner.py        ← LLM planner (Claude API) resolves conflicts
│   ├── scheduler.py      ← priority queue + cooldown logic
│   └── runner.py         ← orchestrates rules → planner → scheduler
│
├── actions/
│   ├── templates.py      ← personalised email/task templates per action type
│   ├── executor.py       ← pulls queue, executes actions, logs outcomes
│   └── feedback.py       ← applies outcome signals back to health scores
│
└── dashboard/
    └── app.jsx           ← React dashboard (standalone, no build step needed)
```

---

## Quickstart

### 1. Clone and install

```bash
git clone https://github.com/yourname/acia.git
cd acia
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Set your Anthropic API key (for LLM planning)

```bash
export ANTHROPIC_API_KEY=sk-ant-your-key-here
```

> The system works without an API key — it falls back to rule-based passthrough. LLM planning enriches reasoning on complex/high-value customers.

### 3. Run the full pipeline

```bash
# Full pipeline — generates data, trains models, runs agent cycle
python main.py

# With Claude API enabled (recommended)
python main.py --llm

# Quiet mode (less output)
python main.py --llm --quiet
```

### 4. Run individual stages

```bash
# Generate data + train models only
python main.py --setup

# Run one agent decision + execution cycle (requires setup first)
python main.py --cycle

# With LLM planning, capped at 10 Claude API calls
python main.py --cycle --llm --llm-budget 10
```

### 5. Start the REST API

```bash
python main.py --api
# → http://localhost:8000/docs  (interactive Swagger UI)
```

---

## REST API endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/` | Health check |
| `GET` | `/dashboard` | Full KPI summary |
| `GET` | `/customers` | List customers with predictions (filterable) |
| `GET` | `/customers/{id}/risk` | Risk profile for one customer |
| `GET` | `/customers/{id}/actions` | Action history for one customer |
| `GET` | `/predictions/churn` | Ranked churn predictions |
| `GET` | `/predictions/conversion` | Ranked conversion predictions |
| `GET` | `/predictions/segments` | Segment distribution + metrics |
| `GET` | `/actions/queue` | Pending / executed action queue |
| `POST` | `/actions/execute` | Execute pending actions |
| `POST` | `/agent/run` | Trigger a full agent decision cycle |
| `POST` | `/agent/train` | Retrain models + re-run inference |
| `POST` | `/agent/feedback` | Apply outcome feedback loop |

Full interactive docs at `http://localhost:8000/docs` when the API is running.

---

## ML models

| Model | Algorithm | Performance | Target |
|-------|-----------|-------------|--------|
| Churn predictor | Random Forest | AUC 0.9985 | `is_churned` label |
| Conversion scorer | Logistic Regression | AUC 1.000 | Plan rank ≥ Pro |
| Segment classifier | KMeans (k=5) | Silhouette 0.23 | RFM cluster |

**Features used (28 total):**
- Customer: tenure, plan rank, MRR, NPS, health score, employee count
- Transaction: count, total spend, AOV, refund rate, days since last
- Engagement: event count (30d/total), session duration, channels used, recency
- Support: ticket count, open/critical tickets, sentiment, cancellation signals
- Email: open rate, click rate
- Derived: engagement index, risk index, recency score

---

## Agent decision rules

| Rule | Priority | Trigger | Action |
|------|----------|---------|--------|
| CHN-001 | P5 Critical | Churn ≥ 80%, MRR ≥ $100 | Escalate to CSM |
| CHN-002 | P4 High | Churn 55–80% | Retention email |
| CHN-003 | P3 Medium | Inactive 30+ days, churn ≥ 35% | Re-engagement email |
| CHN-004 | P4 High | Cancellation ticket or negative sentiment | Proactive support call |
| CNV-001 | P4 High | Conversion ≥ 70%, plan Free/Starter | Upgrade offer |
| CNV-002 | P2 Low | Conversion 40–70%, Free tier | Nurture sequence |
| CNV-003 | P3 Medium | Starter, high usage, conversion ≥ 50% | Upgrade offer → Pro |
| LYL-001 | P2 Low | Champion segment, tenure ≥ 180 days | Loyalty reward |
| LYL-002 | P1 Info | Health score < 35, churn < 45% | Health check-in |

The LLM planner (Claude API) resolves conflicts when multiple rules fire for the same customer, with context including LTV, plan, sentiment, and recent ticket history.

---

## Plugging in real data

ACIA uses SQLite by default. To connect real CRM data:

1. **Replace the SQLite tables** — keep the same schema in `data/generate.py` but populate from your source (Salesforce, HubSpot, CSV exports).

2. **Swap the DB connection** — change `DB_PATH` in `data/features.py` and `api.py`, or replace `sqlite3` with `psycopg2`/`sqlalchemy` for PostgreSQL.

3. **Retrain the models** — call `POST /agent/train` or run `python main.py --setup` after loading real data.

4. **Disable data generation** — comment out the `run_setup()` call in `main.py` and skip straight to `run_cycle()`.

---

## Data generated (synthetic)

| Table | Rows | Description |
|-------|------|-------------|
| `customers` | 500 | Profiles, plan, MRR, health score, churn label |
| `transactions` | ~4,200 | Purchases, renewals, upgrades, refunds |
| `engagement_events` | ~13,000 | Logins, feature usage, sessions |
| `support_tickets` | ~1,200 | Topics, priority, sentiment scores |
| `email_log` | ~3,300 | Campaign emails with open/click tracking |
| `agent_actions` | varies | Actions decided and executed by the agent |
| `ml_predictions` | 500 | Churn score, conversion score, segment, LTV |

---

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | Optional | Claude API key for LLM planning. Falls back to rule passthrough if not set. |

---

## Tech stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.11+ |
| Database | SQLite (swappable to PostgreSQL) |
| ML | scikit-learn, NumPy, pandas |
| LLM | Claude API (claude-sonnet-4-20250514) |
| API | FastAPI + uvicorn |
| Dashboard | React + Recharts (embedded, no build step) |

---

## Outcomes from one full pipeline run

- **500 customers scored** across churn, conversion, and segment
- **281 action candidates** generated by the rule engine
- **243 actions queued** after deduplication and cooldown filtering
- **45% success rate** across executed actions
- **$1.83M LTV** estimated across the customer base
- **76 high-risk customers** (churn score > 60%) flagged for urgent intervention

---

## License

MIT — free to use, modify, and build on.
