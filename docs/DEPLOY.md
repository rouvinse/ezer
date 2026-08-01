# ACIA — Railway Deployment Guide

Deploy ACIA to Railway in under 20 minutes with a live public URL,
PostgreSQL database, and automatic deploys on every git push.

---

## What you'll have at the end

- Live API at `https://your-app.railway.app`
- Interactive docs at `https://your-app.railway.app/docs`
- PostgreSQL database managed by Railway
- Auto-deploys every time you push to GitHub
- Free tier: 500 hours/month (enough for demos and early pilots)
- Paid tier: $5/month for always-on

---

## Prerequisites

- A GitHub account
- A Railway account (free) — https://railway.app
- Your project pushed to a GitHub repository

---

## Step 1 — Push to GitHub

```bash
cd acia

# Initialise git if not done yet
git init
git add .
git commit -m "Initial ACIA deployment"

# Create a repo on github.com, then:
git remote add origin https://github.com/yourusername/acia.git
git push -u origin main
```

Make sure `.gitignore` is in place — it prevents `.env`, `acia.db`,
and `models/artifacts/*.pkl` from being committed.

---

## Step 2 — Create a Railway project

1. Go to https://railway.app and sign in
2. Click **New Project**
3. Select **Deploy from GitHub repo**
4. Authorise Railway to access your GitHub
5. Select your `acia` repository
6. Railway detects the `Procfile` and starts building automatically

---

## Step 3 — Add PostgreSQL database

Inside your Railway project:

1. Click **+ New** → **Database** → **Add PostgreSQL**
2. Railway provisions the database and adds `DATABASE_URL` to your
   environment automatically — no configuration needed
3. Click on your main service → **Variables** and verify
   `DATABASE_URL` is present (Railway injects it automatically
   when a PostgreSQL service is in the same project)

---

## Step 4 — Set environment variables

In your Railway project → main service → **Variables**, add:

```
DB_ENGINE=postgresql

ANTHROPIC_API_KEY=sk-ant-your-key-here
SENDGRID_API_KEY=SG.your-key-here
SENDGRID_FROM_EMAIL=you@yourdomain.com
SENDGRID_FROM_NAME=Your Company Name
```

Railway already sets `DATABASE_URL` and `PORT` automatically.
You do not need to set those manually.

---

## Step 5 — Trigger first deploy

Railway auto-deploys when you push to GitHub. If it hasn't started:

1. Go to your service → **Deployments**
2. Click **Deploy** or push a new commit to trigger it

Watch the build logs — you should see:

```
🚀 ACIA — Production startup sequence
   Database engine : POSTGRESQL
   First boot — generating data + training models...
   ✅ Setup complete
   🔑 ACIA Admin API Key Generated
   Key : acia_admin_sk_xxxxxxxxxx
   ⚠️  Save this key now — it will not be shown again.
   ✅ Startup complete — starting API server
```

**Copy the admin API key from the logs immediately.**
It is shown once and never again. Save it in your `.env` locally:

```
ACIA_ADMIN_KEY=acia_admin_sk_xxxxxxxxxx
```

---

## Step 6 — Verify the deployment

```bash
# Replace with your actual Railway URL
export BASE_URL=https://your-app.railway.app

# Health check
curl $BASE_URL/health

# Should return:
# {"status":"ok","db":"connected","customers":500}

# View interactive API docs
open $BASE_URL/docs
```

---

## Step 7 — Issue your first API key

Use your admin key to issue a key for your first customer (or yourself):

```bash
curl -X POST "$BASE_URL/auth/keys" \
  -H "X-API-Key: acia_admin_sk_xxxxxxxxxx" \
  -H "Content-Type: application/json" \
  -d "company_name=Acme%20Corp&email=admin@acmecorp.com&key_type=live"
```

Response:
```json
{
  "api_key": "acia_live_sk_xxxxxxxx",
  "company_name": "Acme Corp",
  "warning": "Save this key — it will not be shown again."
}
```

---

## Step 8 — Test a protected endpoint

```bash
curl -H "X-API-Key: acia_live_sk_xxxxxxxx" \
     $BASE_URL/dashboard
```

Returns full KPI summary scoped to Acme Corp's data.

---

## Automatic deploys

Every `git push` to `main` triggers a new Railway deploy:

```bash
git add .
git commit -m "Fix: update retention email template"
git push origin main
# Railway auto-deploys in ~2 minutes
```

---

## Running an agent cycle manually

```bash
curl -X POST "$BASE_URL/agent/run?use_llm=true&llm_budget=20" \
     -H "X-API-Key: acia_live_sk_xxxxxxxx"
```

---

## Monitoring

Railway dashboard → your service → **Logs**

Useful things to watch:
- Startup sequence on first boot
- API request logs
- Any errors from the agent cycle

---

## Cost reference

| Tier | Cost | What you get |
|---|---|---|
| Hobby (free) | $0 | 500 hrs/month, sleeps after inactivity |
| Pro | $5/month | Always-on, more resources |
| PostgreSQL | ~$5/month | Managed database (after free credits) |

For demos and early pilots the free tier is sufficient.
For a real pilot customer, upgrade to Pro ($5/month).

---

## Custom domain (optional)

1. Railway project → service → **Settings** → **Domains**
2. Click **Add Custom Domain**
3. Enter `api.yourdomain.com`
4. Add the CNAME record Railway shows you to your DNS provider
5. SSL certificate is issued automatically

---

## Environment variable reference

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | Auto-set by Railway | PostgreSQL connection string |
| `DB_ENGINE` | Yes | Set to `postgresql` |
| `PORT` | Auto-set by Railway | API port |
| `ANTHROPIC_API_KEY` | Optional | Enables LLM planning |
| `SENDGRID_API_KEY` | Optional | Enables real email sending |
| `SENDGRID_FROM_EMAIL` | If SendGrid set | Sender address |
| `SENDGRID_FROM_NAME` | If SendGrid set | Sender display name |

---

## Troubleshooting

**Build fails with "No module named X"**
→ Check `requirements.txt` includes all dependencies

**"table customers does not exist"**
→ The startup sequence will create tables on first boot automatically

**API returns 401 on all requests**
→ Check your `X-API-Key` header is set correctly
→ Issue a new key via `POST /auth/keys` with your admin key

**Agent cycle returns empty results**
→ Run `POST /agent/train` first to score all customers
→ Then `POST /agent/run`

**Logs show "Admin key already exists"**
→ You already saved it from a previous deploy. Check your `.env`
→ If lost, delete the `api_keys` table row where `key_type='admin'`
   and restart the service to regenerate

---

## What to show a potential customer

1. Open `https://your-app.railway.app/docs`
2. Click the lock icon → enter your demo API key
3. Try `GET /dashboard` → shows live KPIs
4. Try `GET /predictions/churn?min_score=0.5` → highest churn risks
5. Try `POST /agent/run` → triggers a full decision cycle
6. Try `GET /approvals/queue` → shows actions waiting for approval

That's your demo. A working AI agent running on a real server,
making real decisions, ready for real customer data.
