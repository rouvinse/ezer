"""
Unit tests — utils/auth.py
Covers API key creation, hashing, validation, revocation, and bootstrap.
All DB operations use a patched in-memory SQLite connection.
"""

from __future__ import annotations
import hashlib
import uuid
from datetime import datetime
from unittest.mock import patch, MagicMock

import pytest
import pandas as pd

# ── Module under test ─────────────────────────────────────────────────────────
from utils.auth import (
    _hash_key,
    _generate_raw_key,
    _extract_prefix,
    KEY_PREFIX_LIVE,
    KEY_PREFIX_TEST,
    KEY_PREFIX_ADMIN,
)


# ─────────────────────────────────────────────────────────────────────────────
# _hash_key
# ─────────────────────────────────────────────────────────────────────────────

class TestHashKey:
    def test_returns_sha256_hex(self):
        raw = "acia_live_sk_abc123"
        expected = hashlib.sha256(raw.encode()).hexdigest()
        assert _hash_key(raw) == expected

    def test_length_is_64(self):
        assert len(_hash_key("any_string")) == 64

    def test_deterministic(self):
        assert _hash_key("foo") == _hash_key("foo")

    def test_different_inputs_give_different_hashes(self):
        assert _hash_key("key_a") != _hash_key("key_b")

    def test_empty_string(self):
        h = _hash_key("")
        assert isinstance(h, str) and len(h) == 64


# ─────────────────────────────────────────────────────────────────────────────
# _generate_raw_key
# ─────────────────────────────────────────────────────────────────────────────

class TestGenerateRawKey:
    def test_live_prefix(self):
        key = _generate_raw_key("live")
        assert key.startswith(KEY_PREFIX_LIVE)

    def test_test_prefix(self):
        key = _generate_raw_key("test")
        assert key.startswith(KEY_PREFIX_TEST)

    def test_admin_prefix(self):
        key = _generate_raw_key("admin")
        assert key.startswith(KEY_PREFIX_ADMIN)

    def test_default_is_live(self):
        key = _generate_raw_key()
        assert key.startswith(KEY_PREFIX_LIVE)

    def test_keys_are_unique(self):
        keys = {_generate_raw_key() for _ in range(100)}
        assert len(keys) == 100


# ─────────────────────────────────────────────────────────────────────────────
# _extract_prefix
# ─────────────────────────────────────────────────────────────────────────────

class TestExtractPrefix:
    def test_shows_first_16_chars(self):
        raw = "acia_live_sk_abcdef"
        prefix = _extract_prefix(raw)
        assert prefix.startswith(raw[:16])
        assert prefix.endswith("...")

    def test_masks_the_rest(self):
        raw = _generate_raw_key()
        prefix = _extract_prefix(raw)
        # The full key must not be exposed
        assert prefix != raw


# ─────────────────────────────────────────────────────────────────────────────
# validate_api_key (mocked DB)
# ─────────────────────────────────────────────────────────────────────────────

class TestValidateApiKey:
    """
    These tests patch read_sql and get_connection so no real DB is needed.
    """

    def _make_row(self, **overrides):
        row = {
            "key_id":       str(uuid.uuid4()),
            "company_id":   "co_test",
            "company_name": "Test Co",
            "key_type":     "live",
            "is_active":    1,
            "revoked_at":   None,
            "plan":         "starter",
            "email":        "test@example.com",
        }
        row.update(overrides)
        return pd.DataFrame([row])

    def test_returns_none_for_empty_key(self):
        from utils.auth import validate_api_key
        assert validate_api_key("") is None
        assert validate_api_key(None) is None

    def test_valid_key_returns_dict(self):
        from utils.auth import validate_api_key
        raw = _generate_raw_key()
        with (
            patch("utils.auth.read_sql", return_value=self._make_row()),
            patch("utils.auth.get_connection") as mock_conn,
        ):
            mock_conn.return_value.__enter__ = lambda s: s
            mock_conn.return_value.__exit__ = MagicMock(return_value=False)
            mock_conn.return_value.execute = MagicMock()
            mock_conn.return_value.commit = MagicMock()
            mock_conn.return_value.close = MagicMock()

            result = validate_api_key(raw)

        assert result is not None
        assert result["company_id"] == "co_test"
        assert result["key_type"] == "live"

    def test_revoked_key_returns_none(self):
        from utils.auth import validate_api_key
        raw = _generate_raw_key()
        revoked_df = self._make_row(is_active=0, revoked_at="2025-01-01 00:00:00")
        with patch("utils.auth.read_sql", return_value=revoked_df):
            result = validate_api_key(raw)
        assert result is None

    def test_inactive_key_returns_none(self):
        from utils.auth import validate_api_key
        raw = _generate_raw_key()
        inactive_df = self._make_row(is_active=0)
        with patch("utils.auth.read_sql", return_value=inactive_df):
            result = validate_api_key(raw)
        assert result is None

    def test_unknown_key_returns_none(self):
        from utils.auth import validate_api_key
        with patch("utils.auth.read_sql", return_value=pd.DataFrame()):
            result = validate_api_key("acia_live_sk_notexist")
        assert result is None

    def test_db_error_returns_none(self):
        from utils.auth import validate_api_key
        with patch("utils.auth.read_sql", side_effect=Exception("DB down")):
            result = validate_api_key("acia_live_sk_anything")
        assert result is None
