"""
ACIA — Authentication Module
Handles API key generation, storage, validation, and revocation.
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

KEY_PREFIX_LIVE  = "acia_live_sk_"
KEY_PREFIX_TEST  = "acia_test_sk_"
KEY_PREFIX_ADMIN = "acia_admin_sk_"
KEY_LENGTH = 32

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
    conn = get_connection()
    conn.executescript(AUTH_SCHEMA)
    conn.commit()
    conn.close()


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def _generate_raw_key(key_type: str = "live") -> str:
    random_part = secrets.token_hex(KEY_LENGTH)
    if key_type == "admin":
        return f"{KEY_PREFIX_ADMIN}{random_part}"
    elif key_type == "test":
        return f"{KEY_PREFIX_TEST}{random_part}"
    return f"{KEY_PREFIX_LIVE}{random_part}"


def _extract_prefix(raw_key: str) -> str:
    return raw_key[:16] + "..."


def create_company(company_name: str, email: str, plan: str = "starter") -> str:
    ensure_auth_schema()
    company_id = str(uuid.uuid4())
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
    ensure_auth_schema()
    try:
        df = read_sql("SELECT company_id FROM companies WHERE email = ?", params=(email,))
        if not df.empty:
            return str(df.iloc[0]["company_id"])
    except Exception:
        pass
    return create_company(company_name, email)


def create_api_key(
    company_name: str,
    email: str,
    key_type: str = "live",
    notes: str = "",
    company_id: str | None = None,
) -> dict:
    ensure_auth_schema()
    company_id = company_id or get_or_create_company(company_name, email)
    raw_key = _generate_raw_key(key_type)
    key_hash = _hash_key(raw_key)
    key_prefix = _extract_prefix(raw_key)
    key_id = str(uuid.uuid4())
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_connection()
    conn.execute(
        "INSERT INTO api_keys "
        "(key_id, company_id, company_name, key_hash, key_prefix, key_type, is_active, created_at, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
        (key_id, company_id, company_name, key_hash, key_prefix, key_type, now, notes)
    )
    conn.commit()
    conn.close()
    return {
        "api_key": raw_key,
        "key_id": key_id,
        "company_id": company_id,
        "company_name": company_name,
        "key_prefix": key_prefix,
        "key_type": key_type,
        "created_at": now,
    }


def validate_api_key(raw_key: str) -> dict | None:
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
    except Exception as e:
        print(f"validate_api_key error: {e}")
        return None

    if df.empty:
        return None

    row = df.iloc[0]

    if not row.get("is_active") or row.get("revoked_at"):
        return None

    # Update last_used_at
    try:
        conn = get_connection()
        conn.execute(
            "UPDATE api_keys SET last_used_at = ? WHERE key_hash = ?",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), key_hash)
        )
        conn.commit()
        conn.close()
    except Exception:
        pass

    return {
        "key_id": str(row.get("key_id", "")),
        "company_id": str(row.get("company_id", "")),
        "company_name": str(row.get("company_name", "")),
        "key_type": str(row.get("key_type", "live")),
        "plan": str(row.get("plan", "starter")),
        "email": str(row.get("email", "")),
    }


def revoke_api_key(key_id: str) -> bool:
    try:
        conn = get_connection()
        conn.execute(
            "UPDATE api_keys SET is_active = 0, revoked_at = ? WHERE key_id = ?",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), key_id)
        )
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False


def list_api_keys(company_id: str | None = None) -> list[dict]:
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


def bootstrap_admin_key() -> str | None:
    """
    Register the permanent admin key from ACIA_ADMIN_KEY env variable.
    Uses ? placeholders — db.py handles PostgreSQL conversion automatically.
    """
    ensure_auth_schema()

    FIXED_KEY = os.environ.get("ACIA_ADMIN_KEY", "")
    if not FIXED_KEY:
        try:
            df = read_sql("SELECT key_id FROM api_keys WHERE key_type = 'admin' AND is_active = 1")
            if not df.empty:
                return None
        except Exception:
            pass
        FIXED_KEY = _generate_raw_key("admin")

    key_hash = _hash_key(FIXED_KEY)

    # Check if already registered
    try:
        df = read_sql("SELECT key_id FROM api_keys WHERE key_hash = ?", params=(key_hash,))
        if not df.empty:
            print(f"   ✅ Admin key already registered")
            return None
    except Exception:
        pass

    # Delete old admin keys
    try:
        conn = get_connection()
        conn.execute("DELETE FROM api_keys WHERE key_type = 'admin'")
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"   ⚠️ Could not clear old admin keys: {e}")

    # Register the fixed key
    try:
        company_id = get_or_create_company("ACIA Admin", "admin@acia.internal")
        key_id = str(uuid.uuid4())
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = get_connection()
        conn.execute(
            "INSERT INTO api_keys "
            "(key_id, company_id, company_name, key_hash, key_prefix, key_type, is_active, created_at, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (key_id, company_id, "ACIA Admin", key_hash, FIXED_KEY[:20], "admin", now, "Permanent admin key")
        )
        conn.commit()
        conn.close()
        print(f"   ✅ Admin key registered: {FIXED_KEY[:25]}...")
    except Exception as e:
        import traceback
        print(f"   ❌ Admin key registration failed: {e}")
        traceback.print_exc()

    return FIXED_KEY


def ensure_admin_key_in_db():
    """Call this on every startup to ensure admin key is always valid."""
    bootstrap_admin_key()


# ── Connector credential storage ───────────────────────────────────────────────
# Credentials are encrypted at rest using AES-256 (Fernet) when the env var
# ACIA_CRED_KEY is set. If not set, they are stored as plain text — acceptable
# for a single-server setup but set the env var in production.

CONNECTOR_CRED_SCHEMA = """
CREATE TABLE IF NOT EXISTS connector_credentials (
    cred_id      TEXT PRIMARY KEY,
    company_id   TEXT NOT NULL,
    connector    TEXT NOT NULL,
    cred_json    TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    UNIQUE(company_id, connector)
);
"""


def _get_fernet():
    """Return a Fernet cipher if ACIA_CRED_KEY is set, else None."""
    key = os.environ.get("ACIA_CRED_KEY", "")
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet
        # Pad/trim key to exactly 32 bytes, then base64-encode for Fernet
        import base64
        raw = key.encode()[:32].ljust(32, b"\x00")
        fernet_key = base64.urlsafe_b64encode(raw)
        return Fernet(fernet_key)
    except Exception:
        return None


def _encrypt_cred(plaintext: str) -> str:
    f = _get_fernet()
    if f:
        return f.encrypt(plaintext.encode()).decode()
    return plaintext


def _decrypt_cred(ciphertext: str) -> str:
    f = _get_fernet()
    if f:
        try:
            return f.decrypt(ciphertext.encode()).decode()
        except Exception:
            return ciphertext  # fallback: return as-is if decryption fails
    return ciphertext


def ensure_connector_cred_schema():
    conn = get_connection()
    conn.executescript(CONNECTOR_CRED_SCHEMA)
    conn.commit()
    conn.close()


def save_connector_credentials(company_id: str, connector: str, creds: dict) -> None:
    """
    Persist connector credentials for a company.
    creds: dict of credential fields (e.g. {"stripe_key": "sk_live_..."})
    """
    import json as _json
    ensure_connector_cred_schema()
    cred_json = _encrypt_cred(_json.dumps(creds))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_connection()
    # Upsert
    existing = conn.execute(
        "SELECT cred_id FROM connector_credentials WHERE company_id=? AND connector=?",
        (company_id, connector)
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE connector_credentials SET cred_json=?, updated_at=? WHERE company_id=? AND connector=?",
            (cred_json, now, company_id, connector)
        )
    else:
        conn.execute(
            "INSERT INTO connector_credentials (cred_id, company_id, connector, cred_json, updated_at) "
            "VALUES (?,?,?,?,?)",
            (str(uuid.uuid4()), company_id, connector, cred_json, now)
        )
    conn.commit()
    conn.close()


def get_connector_credentials(company_id: str, connector: str) -> dict | None:
    """
    Load saved connector credentials for a company.
    Returns the creds dict, or None if not found.
    """
    import json as _json
    ensure_connector_cred_schema()
    try:
        df = read_sql(
            "SELECT cred_json FROM connector_credentials WHERE company_id=? AND connector=?",
            params=(company_id, connector)
        )
        if df.empty:
            return None
        return _json.loads(_decrypt_cred(str(df.iloc[0]["cred_json"])))
    except Exception:
        return None


def list_connected_connectors(company_id: str) -> list[str]:
    """Return list of connector names that have saved credentials."""
    ensure_connector_cred_schema()
    try:
        df = read_sql(
            "SELECT connector FROM connector_credentials WHERE company_id=? ORDER BY updated_at DESC",
            params=(company_id,)
        )
        return df["connector"].tolist() if not df.empty else []
    except Exception:
        return []


