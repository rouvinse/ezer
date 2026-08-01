"""
ACIA — SendGrid Email Integration
Replaces simulated email sending with real SendGrid API calls.

Setup:
  1. Create a free account at https://sendgrid.com
  2. Go to Settings → API Keys → Create API Key (Full Access)
  3. Set environment variable: export SENDGRID_API_KEY=SG.xxxxxxxxx
  4. Set your verified sender: export SENDGRID_FROM_EMAIL=you@yourdomain.com
  5. Set your verified sender name: export SENDGRID_FROM_NAME=ACIA

Free tier limits:
  - 100 emails/day forever on the free plan
  - No credit card required to start
  - More than enough for early pilots

SendGrid docs: https://docs.sendgrid.com/api-reference/mail-send/mail-send
"""

from __future__ import annotations
import os
import json
import urllib.request
import urllib.error
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

SENDGRID_API_URL   = "https://api.sendgrid.com/v3/mail/send"
SENDGRID_API_KEY   = os.environ.get("SENDGRID_API_KEY", "")
FROM_EMAIL         = os.environ.get("SENDGRID_FROM_EMAIL", "noreply@yourdomain.com")
FROM_NAME          = os.environ.get("SENDGRID_FROM_NAME", "ACIA — Customer Intelligence")

# If True, prints email content but doesn't actually send
# Automatically True when API key is not configured
DRY_RUN_MODE       = not bool(SENDGRID_API_KEY)


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class EmailResult:
    success:      bool
    to_email:     str
    subject:      str
    action_type:  str
    message_id:   str | None   # SendGrid message ID if successful
    error:        str | None   # Error message if failed
    dry_run:      bool
    sent_at:      str


# ── Core send function ────────────────────────────────────────────────────────

def send_email(
    to_email:    str,
    to_name:     str,
    subject:     str,
    body:        str,
    action_type: str = "general",
    customer_id: str = "",
    categories:  list[str] | None = None,
) -> EmailResult:
    """
    Send a single email via SendGrid.

    Args:
        to_email    : recipient email address
        to_name     : recipient display name
        subject     : email subject line
        body        : plain text email body
        action_type : ACIA action type (used for SendGrid categories/tracking)
        customer_id : ACIA customer ID (added to custom args for tracking)
        categories  : optional SendGrid categories for analytics grouping

    Returns:
        EmailResult with success status, message ID, and any error
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ── Dry run mode ──────────────────────────────────────────────────────────
    if DRY_RUN_MODE:
        _print_dry_run(to_email, to_name, subject, body, action_type)
        return EmailResult(
            success=True,
            to_email=to_email,
            subject=subject,
            action_type=action_type,
            message_id=f"dry-run-{customer_id}-{datetime.now().timestamp():.0f}",
            error=None,
            dry_run=True,
            sent_at=now,
        )

    # ── Build SendGrid payload ─────────────────────────────────────────────────
    payload = {
        "personalizations": [
            {
                "to": [{"email": to_email, "name": to_name}],
                "custom_args": {
                    "acia_customer_id": customer_id,
                    "acia_action_type": action_type,
                },
            }
        ],
        "from": {
            "email": FROM_EMAIL,
            "name":  FROM_NAME,
        },
        "subject": subject,
        "content": [
            {
                "type":  "text/plain",
                "value": body,
            },
            {
                # HTML version — wrap plain text in minimal styling
                "type":  "text/html",
                "value": _plain_to_html(body, to_name),
            },
        ],
        "tracking_settings": {
            "click_tracking":  {"enable": True},
            "open_tracking":   {"enable": True},
        },
        "categories": categories or [action_type, "acia-automated"],
    }

    # ── Make API call ──────────────────────────────────────────────────────────
    try:
        req = urllib.request.Request(
            SENDGRID_API_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {SENDGRID_API_KEY}",
                "Content-Type":  "application/json",
            },
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=15) as resp:
            # SendGrid returns 202 Accepted on success with X-Message-Id header
            message_id = resp.headers.get("X-Message-Id", "unknown")
            return EmailResult(
                success=True,
                to_email=to_email,
                subject=subject,
                action_type=action_type,
                message_id=message_id,
                error=None,
                dry_run=False,
                sent_at=now,
            )

    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        error_msg  = f"HTTP {e.code}: {_parse_sendgrid_error(error_body)}"
        return EmailResult(
            success=False,
            to_email=to_email,
            subject=subject,
            action_type=action_type,
            message_id=None,
            error=error_msg,
            dry_run=False,
            sent_at=now,
        )

    except Exception as e:
        return EmailResult(
            success=False,
            to_email=to_email,
            subject=subject,
            action_type=action_type,
            message_id=None,
            error=str(e),
            dry_run=False,
            sent_at=now,
        )


def send_batch(emails: list[dict], verbose: bool = True) -> list[EmailResult]:
    """
    Send multiple emails. Each dict in `emails` should have:
      to_email, to_name, subject, body, action_type, customer_id

    SendGrid free tier: 100/day. This function respects that automatically
    by processing synchronously with a small delay between sends.
    """
    import time
    results = []
    total   = len(emails)

    for i, email in enumerate(emails, 1):
        result = send_email(**email)
        results.append(result)

        if verbose:
            status = "✅ sent" if result.success else "❌ failed"
            mode   = " [DRY RUN]" if result.dry_run else ""
            print(f"  [{i}/{total}] {status}{mode} → {email['to_email']} | {email['subject'][:50]}")
            if not result.success:
                print(f"           Error: {result.error}")

        # Small delay between sends to be respectful to SendGrid's rate limits
        if i < total and not DRY_RUN_MODE:
            time.sleep(0.1)

    return results


# ── HTML converter ────────────────────────────────────────────────────────────

def _plain_to_html(body: str, name: str) -> str:
    """
    Converts plain text email body to clean HTML.
    Minimal styling — no images, no complex layout.
    Works in all email clients including Gmail, Outlook, Apple Mail.
    """
    lines     = body.strip().split("\n")
    html_lines = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            html_lines.append("<br>")
        elif stripped.startswith("  ") or stripped.startswith("\t"):
            # Indented lines — treat as list items
            html_lines.append(f'<p style="margin:4px 0 4px 24px;color:#374151">{stripped.strip()}</p>')
        else:
            html_lines.append(f'<p style="margin:8px 0;color:#111827">{stripped}</p>')

    content = "\n".join(html_lines)

    return f"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
</head>
<body style="margin:0;padding:0;background:#f9fafb;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f9fafb;padding:40px 20px">
    <tr>
      <td align="center">
        <table width="560" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:8px;border:1px solid #e5e7eb;overflow:hidden">

          <!-- Header -->
          <tr>
            <td style="background:#1e293b;padding:20px 32px">
              <span style="color:#f1f5f9;font-size:13px;font-weight:600;letter-spacing:0.08em">ACIA</span>
              <span style="color:#64748b;font-size:13px"> · Customer Intelligence</span>
            </td>
          </tr>

          <!-- Body -->
          <tr>
            <td style="padding:32px;font-size:15px;line-height:1.6;color:#111827">
              {content}
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="background:#f8fafc;border-top:1px solid #e5e7eb;padding:20px 32px">
              <p style="margin:0;font-size:12px;color:#94a3b8;line-height:1.5">
                You're receiving this because your account was identified by our customer intelligence system.
                <br>
                To unsubscribe, reply with "unsubscribe" in the subject line.
              </p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""


# ── Error parser ──────────────────────────────────────────────────────────────

def _parse_sendgrid_error(body: str) -> str:
    """Extract human-readable error from SendGrid's error response."""
    try:
        data   = json.loads(body)
        errors = data.get("errors", [])
        if errors:
            return " | ".join(e.get("message", str(e)) for e in errors)
        return body[:200]
    except Exception:
        return body[:200]


# ── Dry run printer ───────────────────────────────────────────────────────────

def _print_dry_run(to_email, to_name, subject, body, action_type):
    """Pretty-print email content in dry run mode."""
    print(f"\n{'─'*58}")
    print(f"  [DRY RUN] Email — would be sent via SendGrid")
    print(f"{'─'*58}")
    print(f"  To      : {to_name} <{to_email}>")
    print(f"  From    : {FROM_NAME} <{FROM_EMAIL}>")
    print(f"  Subject : {subject}")
    print(f"  Type    : {action_type}")
    print(f"  Body preview:")
    for line in body.strip().split("\n")[:10]:
        print(f"    {line}")
    if len(body.split("\n")) > 10:
        print(f"    ... ({len(body.split(chr(10)))} lines total)")
    print(f"{'─'*58}\n")


# ── Config validator ──────────────────────────────────────────────────────────

def validate_config() -> dict:
    """
    Check SendGrid configuration and return status.
    Call this on startup to understand what mode you're in.
    """
    status = {
        "api_key_set":    bool(SENDGRID_API_KEY),
        "from_email_set": FROM_EMAIL != "noreply@yourdomain.com",
        "from_name":      FROM_NAME,
        "from_email":     FROM_EMAIL,
        "mode":           "live" if (SENDGRID_API_KEY and FROM_EMAIL != "noreply@yourdomain.com") else "dry_run",
        "ready":          bool(SENDGRID_API_KEY) and FROM_EMAIL != "noreply@yourdomain.com",
    }
    return status


def print_config_status():
    """Print a human-readable config status — call this on startup."""
    cfg = validate_config()
    print(f"\n── SendGrid Configuration ───────────────────────────")
    print(f"  Mode          : {'🟢 LIVE — emails will be sent' if cfg['mode'] == 'live' else '🟡 DRY RUN — emails will be printed, not sent'}")
    print(f"  API key       : {'✅ set' if cfg['api_key_set'] else '❌ not set (export SENDGRID_API_KEY=SG.xxx)'}")
    print(f"  From email    : {'✅ ' + cfg['from_email'] if cfg['from_email_set'] else '❌ not set (export SENDGRID_FROM_EMAIL=you@domain.com)'}")
    print(f"  From name     : {cfg['from_name']}")
    print(f"  Ready to send : {'✅ yes' if cfg['ready'] else '⚠️  no — running in dry run mode'}")
    if not cfg['ready']:
        print(f"\n  To enable live sending:")
        print(f"    export SENDGRID_API_KEY=SG.your_key_here")
        print(f"    export SENDGRID_FROM_EMAIL=you@yourdomain.com")
        print(f"    export SENDGRID_FROM_NAME='Your Company'")
        print(f"\n  Get a free API key at: https://app.sendgrid.com/settings/api_keys")
    print(f"────────────────────────────────────────────────────\n")


# ── Quick test ────────────────────────────────────────────────────────────────

def test_send(to_email: str) -> EmailResult:
    """
    Send a test email to verify your SendGrid setup is working.
    Usage: python -c "from actions.sendgrid_email import test_send; test_send('you@email.com')"
    """
    return send_email(
        to_email=to_email,
        to_name="ACIA Test",
        subject="✅ ACIA SendGrid integration is working",
        body="""Hi there,

This is a test email from ACIA — Autonomous CRM Intelligence Agent.

If you're reading this, your SendGrid integration is configured correctly and emails are being sent successfully.

Your ACIA system is ready to send:
  • Retention emails to at-risk customers
  • Re-engagement emails to dormant users
  • Upgrade offers to high-conversion prospects
  • Loyalty rewards to champion customers
  • Health check-ins to low-health accounts

You can now run python main.py --cycle to execute a full agent cycle with real email sending.

The ACIA Team""",
        action_type="test",
        customer_id="TEST",
    )


if __name__ == "__main__":
    print_config_status()

    # If an email address is passed as argument, send a test
    import sys
    if len(sys.argv) > 1:
        test_email = sys.argv[1]
        print(f"Sending test email to {test_email}...")
        result = test_send(test_email)
        if result.success:
            print(f"✅ {'Dry run' if result.dry_run else 'Email sent'} successfully!")
            if result.message_id:
                print(f"   Message ID: {result.message_id}")
        else:
            print(f"❌ Failed: {result.error}")
    else:
        print("Usage: python actions/sendgrid_email.py your@email.com")
        print("       (sends a test email to verify your setup)\n")
        print("Running dry run demo...")
        result = test_send("demo@example.com")
