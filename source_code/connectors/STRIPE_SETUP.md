# EZER — Stripe Connector Setup Guide

This guide walks you through connecting your Stripe account to EZER
so your real customer and revenue data flows into the intelligence engine.

---

## What EZER pulls from Stripe

| Data | Maps to |
|---|---|
| Customers (name, email, country) | customers table |
| Subscriptions (plan, MRR, status) | customers table |
| Invoices (payments, renewals, refunds) | transactions table |
| Churn events (canceled subscriptions) | customers.is_churned |

---

## Step 1 — Get your Stripe Secret Key

1. Go to **dashboard.stripe.com/apikeys**
2. Copy your **Secret key** — starts with `sk_live_` (production) or `sk_test_` (testing)
3. Never share this key or commit it to GitHub

**Start with your test key (`sk_test_...`) first** — it pulls your test data
safely. Switch to live key when you're ready for real data.

---

## Step 2 — Set the environment variable

```bash
# On your local machine
export STRIPE_SECRET_KEY=sk_test_your_key_here

# On Railway (recommended for production)
# Go to your Railway project → Variables → Add:
# STRIPE_SECRET_KEY = sk_live_your_key_here
```

---

## Step 3 — Run the sync

```bash
# Test connection first
python connectors/stripe_connector.py --test

# Sync all customers and transactions
python connectors/stripe_connector.py --sync --company-id your-company-id

# Sync without invoice history (faster)
python connectors/stripe_connector.py --sync --no-invoices --company-id your-company-id

# Check what's in the database
python connectors/stripe_connector.py --status --company-id your-company-id
```

---

## Step 4 — Run EZER on your real data

Once the sync completes, run the full pipeline:

```bash
# Train ML models on your real customer data
python main.py --setup

# Run the agent decision cycle
python main.py --cycle --llm

# Start the API
python main.py --api
```

---

## Step 5 — Set up automatic re-sync (optional)

Add a cron job or Railway scheduled task to re-sync daily:

```bash
# Runs every night at midnight
0 0 * * * cd /your/ezer/path && python connectors/stripe_connector.py --sync --company-id your-id
```

Or use the API endpoint to trigger sync manually:

```bash
curl -X POST "https://ezer-production.up.railway.app/connectors/stripe/sync" \
     -H "X-API-Key: your-api-key" \
     -H "Content-Type: application/json" \
     -d '{"fetch_invoices": true}'
```

---

## API Endpoints

| Endpoint | What it does |
|---|---|
| `POST /connectors/stripe/sync` | Trigger a full Stripe sync |
| `GET /connectors/stripe/status` | Show current data in EZER |
| `POST /connectors/stripe/test` | Test Stripe connection |

---

## What happens after sync

EZER will have your real customers loaded. The ML models will train on
your actual data and start scoring everyone for churn risk and upgrade
probability. The agent will queue actions based on what it finds.

**First run — what to expect:**
- All customers get an initial health score based on subscription status
- Customers with active subscriptions: health score ~75
- Customers with past due payments: health score ~30
- Churned customers: health score ~10
- As more data comes in (support tickets, emails, usage), scores improve

**Missing data signals:**
The Stripe connector provides billing signals only. For a complete picture add:
- Support tickets → Intercom or Freshdesk connector
- Email engagement → SendGrid connector
- Product usage → Mixpanel or Amplitude connector

Each additional connector improves prediction accuracy significantly.

---

## Troubleshooting

**"STRIPE_SECRET_KEY not set"**
→ Run: `export STRIPE_SECRET_KEY=sk_test_...`

**"No such module: stripe"**
→ Run: `pip install stripe`

**"Invalid API Key provided"**
→ Double-check your key — make sure you copied the full string

**Customers showing MRR = 0**
→ They are on a free plan or their subscription is inactive in Stripe

**Sync takes a long time**
→ Normal if you have many customers. Use `--no-invoices` for a faster initial sync.
