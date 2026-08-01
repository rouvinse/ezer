"""
ACIA — Action Templates
Generates personalised email/message content for each action type.
Returns structured template objects consumed by the executor.
"""

from __future__ import annotations
import random
from dataclasses import dataclass


@dataclass
class MessageTemplate:
    action_type:  str
    subject:      str
    body:         str
    channel:      str        # email | slack_csm | internal_task
    cta:          str        # call-to-action label
    discount_pct: int = 0    # 0 = no discount


# ── Discount code generator ───────────────────────────────────────────────────
def _discount_code(pct: int, customer_id: str) -> str:
    suffix = customer_id[-4:].upper()
    return f"SAVE{pct}-{suffix}"


# ── Template builders ─────────────────────────────────────────────────────────

def retention_email(customer: dict, churn_score: float) -> MessageTemplate:
    name    = customer.get("name", "there")
    plan    = customer.get("plan", "plan")
    mrr     = customer.get("mrr", 0)
    disc    = 20 if mrr >= 200 else 15
    code    = _discount_code(disc, customer["customer_id"])

    return MessageTemplate(
        action_type="send_retention_email",
        subject=f"We'd love to keep you, {name.split()[0]} — exclusive offer inside",
        body=f"""Hi {name.split()[0]},

We noticed you haven't been as active lately, and we want to make sure
you're getting the full value from your {plan} plan.

As a thank-you for being with us, here's an exclusive offer:

  🎁  {disc}% off your next 3 months — use code {code} at checkout

This offer expires in 7 days. We'd also love to hear if there's anything
we can do better — reply to this email and our team will personally respond.

We're here for you,
The ACIA Team""",
        channel="email",
        cta=f"Claim {disc}% Off",
        discount_pct=disc,
    )


def reengagement_email(customer: dict, days_inactive: int) -> MessageTemplate:
    name = customer.get("name", "there")
    plan = customer.get("plan", "plan")

    tips = [
        "You can now export reports with a single click — try it today.",
        "Our new AI-powered dashboard is live — see what's changed.",
        "3 features your team might have missed — worth 5 minutes of your time.",
    ]
    tip = random.choice(tips)

    return MessageTemplate(
        action_type="send_reengagement_email",
        subject=f"We miss you, {name.split()[0]} — here's what's new",
        body=f"""Hi {name.split()[0]},

It's been {days_inactive} days since we've seen you, and a lot has changed.

{tip}

Your {plan} account is still active and ready when you are.
Log in and pick up where you left off — it only takes a minute.

Talk soon,
The ACIA Team""",
        channel="email",
        cta="Log Back In",
    )


def upgrade_offer_email(customer: dict, suggested_plan: str, conversion_score: float) -> MessageTemplate:
    name     = customer.get("name", "there")
    cur_plan = customer.get("plan", "current plan")
    disc     = 25 if conversion_score >= 0.80 else 15
    code     = _discount_code(disc, customer["customer_id"])

    features = {
        "Starter":    ["unlimited projects", "priority support", "team collaboration"],
        "Pro":        ["advanced analytics", "custom integrations", "dedicated onboarding"],
        "Enterprise": ["SSO & SAML", "SLAs", "custom contracts", "a dedicated CSM"],
    }
    feat_list = "\n".join(
        f"  ✓  {f}" for f in features.get(suggested_plan, ["premium features"])
    )

    return MessageTemplate(
        action_type="send_upgrade_offer",
        subject=f"You've outgrown {cur_plan} — unlock {suggested_plan} for {disc}% off",
        body=f"""Hi {name.split()[0]},

Based on how you've been using the platform, you're ready for more.

{suggested_plan} gives you:
{feat_list}

For a limited time, upgrade and save {disc}% for 6 months with code {code}.

It takes 2 minutes to switch — no data migration needed.

Let's grow together,
The ACIA Team""",
        channel="email",
        cta=f"Upgrade to {suggested_plan}",
        discount_pct=disc,
    )


def loyalty_reward_email(customer: dict, tenure_days: int) -> MessageTemplate:
    name   = customer.get("name", "there")
    years  = round(tenure_days / 365, 1)
    code   = _discount_code(10, customer["customer_id"])

    return MessageTemplate(
        action_type="send_loyalty_reward",
        subject=f"Thank you for {years} years, {name.split()[0]} 🎉",
        body=f"""Hi {name.split()[0]},

{years} years. That's how long you've been part of our community,
and we don't take that for granted.

As a token of our appreciation:
  🎁  10% off your next renewal — automatically applied with code {code}
  ⭐  Early access to our upcoming features
  📞  Priority support queue for the next 3 months

You're one of our Champions, and we mean that.

With gratitude,
The ACIA Team""",
        channel="email",
        cta="Claim Your Reward",
        discount_pct=10,
    )


def health_checkin_email(customer: dict, health_score: float) -> MessageTemplate:
    name = customer.get("name", "there")
    plan = customer.get("plan", "plan")

    return MessageTemplate(
        action_type="send_health_checkin",
        subject=f"Quick check-in — are you getting value from {plan}?",
        body=f"""Hi {name.split()[0]},

We like to reach out to customers from time to time to make sure
everything's going smoothly.

How are things going? Is there anything that's been frustrating,
unclear, or could work better for your team?

Our Customer Success team reads every reply — no bots, no scripts.
Hit reply and let us know. We're listening.

Warmly,
The ACIA Team""",
        channel="email",
        cta="Share Feedback",
    )


def csm_escalation_task(customer: dict, churn_score: float, ltv: float) -> MessageTemplate:
    name = customer.get("name", "there")
    plan = customer.get("plan", "plan")

    return MessageTemplate(
        action_type="escalate_to_csm",
        subject=f"[URGENT] CSM Required — {customer['customer_id']} churn risk {churn_score:.0%}",
        body=f"""ACTION REQUIRED — Customer Success Manager

Customer  : {name} ({customer['customer_id']})
Plan      : {plan}  |  MRR: ${customer.get('mrr', 0):.0f}/mo
Churn Risk: {churn_score:.0%}
LTV Est.  : ${ltv:,.0f}

Recommended actions:
  1. Call within 24 hours — introduce yourself, listen first
  2. Identify pain points from recent support tickets
  3. Offer executive business review or custom success plan
  4. Escalate to retention discount if needed (up to 30%)

This alert was generated autonomously by ACIA.
Log outcome in CRM within 48 hours.""",
        channel="slack_csm",
        cta="Log Outcome",
    )


def support_call_task(customer: dict, sentiment: float, open_tickets: int) -> MessageTemplate:
    name = customer.get("name", "there")
    urgency = "URGENT" if sentiment < -0.5 else "SCHEDULED"

    return MessageTemplate(
        action_type="proactive_support_call",
        subject=f"[{urgency}] Proactive call — {customer['customer_id']}",
        body=f"""Support Team Task

Customer    : {name} ({customer['customer_id']})
Plan        : {customer.get('plan', 'Unknown')}
Open Tickets: {int(open_tickets)}
Sentiment   : {sentiment:.2f} ({'Negative' if sentiment < 0 else 'Neutral/Positive'})

Schedule a proactive outbound call:
  • Review open tickets and confirm resolution
  • Ask if there's anything blocking their workflow
  • Document conversation and close all resolved tickets

Target: within {'48' if urgency == 'URGENT' else '72'} hours.""",
        channel="internal_task",
        cta="Schedule Call",
    )


def nurture_enroll_task(customer: dict, conversion_score: float) -> MessageTemplate:
    name = customer.get("name", "there")
    plan = customer.get("plan", "Free")

    return MessageTemplate(
        action_type="enroll_nurture_sequence",
        subject=f"Enrol {customer['customer_id']} in nurture sequence",
        body=f"""Marketing Automation Task

Customer       : {name} ({customer['customer_id']})
Current Plan   : {plan}
Conv. Score    : {conversion_score:.0%}

Action: Enrol in 5-email nurture sequence
  Day 1  — Welcome + quick-start guide
  Day 3  — Feature spotlight: top 3 use cases
  Day 7  — Customer success story (similar industry)
  Day 14 — Limited-time upgrade offer (15% off)
  Day 21 — Personal check-in from account manager""",
        channel="internal_task",
        cta="Enrol Now",
    )


# ── Template dispatcher ───────────────────────────────────────────────────────

def build_template(action: dict, customer: dict, prediction: dict) -> MessageTemplate:
    """
    Route action_type to the correct template builder.
    action     : row from agent_actions table
    customer   : row from customers table
    prediction : row from ml_predictions table
    """
    atype          = action["action_type"]
    churn_score    = float(prediction.get("churn_score", 0))
    conv_score     = float(prediction.get("conversion_score", 0))
    ltv            = float(prediction.get("ltv_estimate", 0))
    health_score   = float(customer.get("health_score", 50))
    days_inactive  = int(customer.get("days_since_last_event", 0))
    open_tickets   = int(customer.get("open_tickets", 0))
    sentiment      = float(customer.get("avg_sentiment", 0))
    tenure_days    = int(customer.get("tenure_days", 0))
    suggested_plan = action.get("context_plan") or (
        "Pro" if customer.get("plan") == "Starter" else "Starter"
    )

    dispatch = {
        "send_retention_email":    lambda: retention_email(customer, churn_score),
        "send_reengagement_email": lambda: reengagement_email(customer, days_inactive),
        "send_upgrade_offer":      lambda: upgrade_offer_email(customer, suggested_plan, conv_score),
        "send_loyalty_reward":     lambda: loyalty_reward_email(customer, tenure_days),
        "send_health_checkin":     lambda: health_checkin_email(customer, health_score),
        "escalate_to_csm":         lambda: csm_escalation_task(customer, churn_score, ltv),
        "proactive_support_call":  lambda: support_call_task(customer, sentiment, open_tickets),
        "enroll_nurture_sequence": lambda: nurture_enroll_task(customer, conv_score),
    }

    builder = dispatch.get(atype)
    if builder:
        return builder()

    # Fallback generic
    return MessageTemplate(
        action_type=atype,
        subject=f"Action: {atype} for {customer['customer_id']}",
        body=f"Please review and action: {action.get('reason', '')}",
        channel="internal_task",
        cta="Review",
    )
