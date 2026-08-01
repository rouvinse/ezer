# SendGrid Setup Guide — ACIA Email Integration

## What this enables
Once configured, ACIA will send real personalised emails to customers
via SendGrid instead of simulating them. This includes:
- Retention emails to at-risk customers
- Re-engagement emails to dormant users
- Upgrade offers to high-conversion prospects
- Loyalty rewards to champion customers
- Health check-ins to low-health accounts

---

## Step 1 — Create a free SendGrid account

Go to: https://signup.sendgrid.com

- No credit card required
- Free tier: 100 emails/day, forever
- More than enough for early pilots and testing

---

## Step 2 — Verify a sender identity

SendGrid requires you to verify the email address you send FROM.

Option A — Single sender (easiest to start):
  1. Go to Settings → Sender Authentication
  2. Click "Verify a Single Sender"
  3. Fill in your name, email address, and company details
  4. Check your inbox and click the verification link
  5. Done — takes 2 minutes

Option B — Domain authentication (recommended for production):
  1. Go to Settings → Sender Authentication
  2. Click "Authenticate Your Domain"
  3. Add DNS records to your domain (SendGrid provides exact records)
  4. Improves deliverability significantly

---

## Step 3 — Create an API key

1. Go to Settings → API Keys
2. Click "Create API Key"
3. Name it "ACIA Production" or similar
4. Select "Full Access" (or "Restricted Access" → Mail Send → Full Access)
5. Click "Create & View"
6. COPY THE KEY NOW — SendGrid only shows it once

---

## Step 4 — Set environment variables

In your terminal, before running ACIA:

  export SENDGRID_API_KEY=SG.your_key_here
  export SENDGRID_FROM_EMAIL=you@yourdomain.com
  export SENDGRID_FROM_NAME="Your Company Name"

To make these permanent, add them to your ~/.bashrc or ~/.zshrc:

  echo 'export SENDGRID_API_KEY=SG.your_key_here' >> ~/.bashrc
  echo 'export SENDGRID_FROM_EMAIL=you@yourdomain.com' >> ~/.bashrc
  echo 'export SENDGRID_FROM_NAME="Your Company"' >> ~/.bashrc
  source ~/.bashrc

---

## Step 5 — Test the integration

Send a test email to yourself:

  cd /path/to/acia
  python3 actions/sendgrid_email.py your@email.com

You should receive a test email within 30 seconds.

Check the config status at any time:

  python3 -c "from actions.sendgrid_email import print_config_status; print_config_status()"

---

## Step 6 — Run ACIA with real email sending

  python main.py --cycle

ACIA will now send real emails for all email-type actions.
Non-email actions (CSM escalations, support calls) remain simulated
until those integrations are built (Slack, calendar tools, etc.)

---

## Dry run mode

If SENDGRID_API_KEY is not set, ACIA automatically runs in dry run mode:
- Email content is printed to the terminal
- Nothing is actually sent
- Everything else works normally
- Safe for development and testing

---

## Monitoring sends in SendGrid

Once emails are going out, monitor them at:
https://app.sendgrid.com/statistics

You'll see:
- Delivery rate
- Open rate
- Click rate
- Bounce rate
- Unsubscribes

These metrics will eventually feed back into ACIA's outcome tracking
when webhook integration is added.

---

## SendGrid webhooks (next step after this)

SendGrid can notify ACIA when an email is opened or clicked via webhooks.
This will replace the current placeholder open/click tracking with real data.

Setup (for later):
  1. Go to Settings → Mail Settings → Event Webhook
  2. Point it at: https://your-domain.com/webhooks/sendgrid
  3. Select: Opens, Clicks, Bounces, Unsubscribes
  4. Build the /webhooks/sendgrid endpoint in api.py

This is the next integration milestone after basic sending is working.

---

## Troubleshooting

"403 Forbidden" error:
  → API key doesn't have Mail Send permission
  → Create a new key with Full Access

"The from address does not match a verified sender":
  → Complete sender verification in Step 2 above

Emails going to spam:
  → Use domain authentication (Step 2, Option B) instead of single sender
  → Check your email content for spam trigger words
  → Ensure unsubscribe link is present (already included in ACIA templates)

"Daily limit exceeded":
  → You've hit the 100/day free tier limit
  → Upgrade to Essentials plan ($19.95/month for 50,000 emails)
  → Or throttle ACIA to send fewer emails per cycle

---

## Cost reference

Free:          100 emails/day    — development, testing, first pilot
Essentials:    $19.95/month      — 50,000 emails/month
Pro:           $89.95/month      — 100,000 emails/month

At 500 customers with ACIA running daily cycles, you need roughly
500–1,000 emails/month maximum, well within the free tier.
