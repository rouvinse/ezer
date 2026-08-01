"""
ACIA — Authentication Module
Handles API key generation, storage, validation, and revocation.

How it works:
  1. A company signs up → you call create_api_key("Company Name")
  2. It generates a secure random key: acia_live_sk_xxxxxxxxxxxxx
  3. The key is hashed (SHA-256) and stored in the api_keys table
  4. The raw key is shown ONCE — you give it to the customer
  5. Every API request includes the key in the Authorization header
  6. ACIA hashes the incoming key and checks it against the DB
  7. Match → request allowed. No match → 401 Unauthorized.

Why hashing?
  Even if your database is compromised, attackers can't use
  the hashed keys. Only the raw key (which you never store) works.
"""

from __future__ import annotations
import os
import secrets
import hashlib
import uuid
from datetime import datetime
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.db import get_connection, read_sql

# ── Key format ────────────────────────────────────────────────────────────────
# acia_live_sk_   → production key
# acia_test_sk_   → test/sandbox key
# acia_admin_sk_  → admin key (full access)

KEY_PREFIX_LIVE  = "acia_live_sk_"
KEY_PREFIX_TEST  = "acia_test_sk_"
KEY_PREFIX_ADMIN = "acia_admin_sk_"

KEY_LENGTH = 32   # bytes of randomness → 64 hex chars after prefix


# ── Schema ────────────────────────────────────────────────────────────────────

AUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
    key_id        TEXT PRIMARY KEY,
    company_id    TEXT NOT NULL,
    company_name  TEXT NOT NULL,
    key_hash      TEXT NOT NULL UNIQUE,
    key_prefix    TEXT NOT NULL,
    key_type      TEXT NOT NULL DEFAULT 'live',
    is_active     INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    last_used_at  TEXT,
    revoked_at    TEXT,
    notes         TEXT
);

CREATE TABLE IF NOT EXISTS companies (
    company_id    TEXT PRIMARY KEY,
    company_name  TEXT NOT NULL,
    email         TEXT UNIQUE,
    plan          TEXT DEFAULT 'starter',
    is_active     INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL
);
"""


def ensure_auth_schema():
    """Create auth tables if they don't exist."""
    conn = get_connection()
    conn.executescript(AUTH_SCHEMA)
    conn.commit()
    conn.close()


# ── Key generation ────────────────────────────────────────────────────────────

def _generate_raw_key(key_type: str = "live") -> str:
    """Generate a secure random API key."""
    random_part = secrets.token_hex(KEY_LENGTH)
    if key_type == "admin":
        return f"{KEY_PREFIX_ADMIN}{random_part}"
    elif key_type == "test":
        return f"{KEY_PREFIX_TEST}{random_part}"
    return f"{KEY_PREFIX_LIVE}{random_part}"


def _hash_key(raw_key: str) -> str:
    """SHA-256 hash of the raw key — this is what gets stored."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


def _extract_prefix(raw_key: str) -> str:
    """Extract the displayable prefix (first 16 chars) for identification."""
    return raw_key[:16] + "..."


# ── Company management ────────────────────────────────────────────────────────

def create_company(
    company_name: str,
    email:        str,
    plan:         str = "starter",
) -> str:
    """Register a new company. Returns company_id."""
    ensure_auth_schema()
    company_id = str(uuid.uuid4())
    now        = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_connection()
    conn.execute(
        "INSERT INTO companies (company_id, company_name, email, plan, is_active, created_at) "
        "VALUES (?, ?, ?, ?, 1, ?)",
        (company_id, company_name, email, plan, now)
    )
    conn.commit()
    conn.close()
    return company_id


def get_or_create_company(company_name: str, email: str) -> str:
    """Get existing company_id or create new company."""
    ensure_auth_schema()
    try:
        df = read_sql("SELECT company_id FROM companies WHERE email = ?", params=(email,))
        if not df.empty:
            return df.iloc[0]["company_id"]
    except Exception:
        pass
    return create_company(company_name, email)


# ── API key management ─────────────────────────────────────────────────────────

def create_api_key(
    company_name: str,
    email:        str,
    key_type:     str = "live",
    notes:        str = "",
) -> dict:
    """
    Create a new API key for a company.

    Returns:
        {
            "api_key":      "acia_live_sk_xxx..."  ← show this ONCE to customer
            "key_id":       "uuid",
            "company_id":   "uuid",
            "company_name": "Acme Corp",
            "key_type":     "live",
            "created_at":   "2025-01-01 00:00:00",
        }

    IMPORTANT: The raw api_key is never stored. Show it to the customer
    and tell them to save it. It cannot be recovered if lost.
    """
    ensure_auth_schema()

    company_id = get_or_create_company(company_name, email)
    raw_key    = _generate_raw_key(key_type)
    key_hash   = _hash_key(raw_key)
    key_prefix = _extract_prefix(raw_key)
    key_id     = str(uuid.uuid4())
    now        = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_connection()
    conn.execute(
        "INSERT INTO api_keys "
        "(key_id, company_id, company_name, key_hash, key_prefix, key_type, is_active, created_at, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
        (key_id, company_id, company_name, key_hash, key_prefix,
         key_type, now, notes)
    )
    conn.commit()
    conn.close()

    return {
        "api_key":      raw_key,       # ← SHOW ONCE, never store
        "key_id":       key_id,
        "company_id":   company_id,
        "company_name": company_name,
        "key_prefix":   key_prefix,    # for display/identification
        "key_type":     key_type,
        "created_at":   now,
    }


def validate_api_key(raw_key: str) -> dict | None:
    """
    Validate an incoming API key.

    Returns company info dict if valid, None if invalid/revoked.
    Also updates last_used_at timestamp.
    """
    if not raw_key:
        return None

    key_hash = _hash_key(raw_key)

    try:
        df = read_sql(
            "SELECT k.key_id, k.company_id, k.company_name, k.key_type, "
            "k.is_active, k.revoked_at, c.plan, c.email "
            "FROM api_keys k "
            "JOIN companies c ON c.company_id = k.company_id "
            "WHERE k.key_hash = ?",
            params=(key_hash,)
        )
    except Exception:
        return None

    if df.empty:
        return None

    row = df.iloc[0]

    # Check if active
    if not row.get("is_active") or row.get("revoked_at"):
        return None

    # Update last_used_at
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = get_connection()
        conn.execute(
            "UPDATE api_keys SET last_used_at = ? WHERE key_hash = ?",
            (now, key_hash)
        )
        conn.commit()
        conn.close()
    except Exception:
        pass

    return {
        "key_id":       str(row.get("key_id", "")),
        "company_id":   str(row.get("company_id", "")),
        "company_name": str(row.get("company_name", "")),
        "key_type":     str(row.get("key_type", "live")),
        "plan":         str(row.get("plan", "starter")),
        "email":        str(row.get("email", "")),
    }


def revoke_api_key(key_id: str) -> bool:
    """Revoke an API key by key_id. Returns True if successful."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = get_connection()
        conn.execute(
            "UPDATE api_keys SET is_active = 0, revoked_at = ? WHERE key_id = ?",
            (now, key_id)
        )
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False


def list_api_keys(company_id: str | None = None) -> list[dict]:
    """List all API keys, optionally filtered by company."""
    try:
        if company_id:
            df = read_sql(
                "SELECT key_id, company_id, company_name, key_prefix, key_type, "
                "is_active, created_at, last_used_at, revoked_at "
                "FROM api_keys WHERE company_id = ? ORDER BY created_at DESC",
                params=(company_id,)
            )
        else:
            df = read_sql(
                "SELECT key_id, company_id, company_name, key_prefix, key_type, "
                "is_active, created_at, last_used_at, revoked_at "
                "FROM api_keys ORDER BY created_at DESC"
            )
        return df.to_dict("records")
    except Exception:
        return []


# ── Admin key bootstrap ───────────────────────────────────────────────────────

def bootstrap_admin_key() -> str | None:
    """
    Create the initial admin API key if none exists.
    Called once on first startup.
    Prints the key to console — save it immediately.
    """
    ensure_auth_schema()

    # Check if admin key already exists
    try:
        df = read_sql("SELECT key_id FROM api_keys WHERE key_type = 'admin' AND is_active = 1")
        if not df.empty:
            return None   # Already exists
    except Exception:
        pass

    result = create_api_key(
        company_name="ACIA Admin",
        email="admin@acia.internal",
        key_type="admin",
        notes="Auto-generated admin key on first startup",
    )

    print(f"\n{'='*56}")
    print(f"  🔑  ACIA Admin API Key Generated")
    print(f"{'='*56}")
    print(f"  Key : {result['api_key']}")
    print(f"{'='*56}")
    print(f"  ⚠️  Save this key now — it will not be shown again.")
    print(f"  Add it to your .env file:")
    print(f"  ACIA_ADMIN_KEY={result['api_key']}")
    print(f"{'='*56}\n")

    return result["api_key"]


# ── CLI for key management ────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import argparse

    parser = argparse.ArgumentParser(description="ACIA API Key Management")
    sub    = parser.add_subparsers(dest="command")

    # create
    p_create = sub.add_parser("create", help="Create a new API key")
    p_create.add_argument("--company", required=True, help="Company name")
    p_create.add_argument("--email",   required=True, help="Company email")
    p_create.add_argument("--type",    default="live", choices=["live","test","admin"])
    p_create.add_argument("--notes",   default="")

    # revoke
    p_revoke = sub.add_parser("revoke", help="Revoke an API key")
    p_revoke.add_argument("key_id", help="Key ID to revoke")

    # list
    p_list = sub.add_parser("list", help="List all API keys")
    p_list.add_argument("--company-id", default=None)

    # validate
    p_val = sub.add_parser("validate", help="Validate an API key")
    p_val.add_argument("api_key", help="Raw API key to validate")

    # bootstrap
    sub.add_parser("bootstrap", help="Create initial admin key")

    args = parser.parse_args()

    if args.command == "create":
        result = create_api_key(args.company, args.email, args.type, args.notes)
        print(f"\n✅ API Key Created")
        print(f"   Company : {result['company_name']}")
        print(f"   Type    : {result['key_type']}")
        print(f"   Key ID  : {result['key_id']}")
        print(f"\n   🔑  API Key (save this — shown once):")
        print(f"   {result['api_key']}\n")

    elif args.command == "revoke":
        success = revoke_api_key(args.key_id)
        print(f"{'✅ Revoked' if success else '❌ Failed'}: {args.key_id}")

    elif args.command == "list":
        keys = list_api_keys(args.company_id)
        if not keys:
            print("No API keys found.")
        else:
            print(f"\n{'Key ID':38} {'Company':20} {'Type':6} {'Active':6} {'Last Used'}")
            print("─" * 90)
            for k in keys:
                active    = "✅" if k.get("is_active") else "❌"
                last_used = k.get("last_used_at") or "never"
                print(f"  {k['key_id']:36} {k['company_name']:20} {k['key_type']:6} {active:6} {last_used}")

    elif args.command == "validate":
        result = validate_api_key(args.api_key)
        if result:
            print(f"✅ Valid key")
            print(f"   Company : {result['company_name']}")
            print(f"   Plan    : {result['plan']}")
            print(f"   Type    : {result['key_type']}")
        else:
            print("❌ Invalid or revoked key")

    elif args.command == "bootstrap":
        key = bootstrap_admin_key()
        if not key:
            print("Admin key already exists. Use 'list' to see it.")

    else:
        parser.print_help()
