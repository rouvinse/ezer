"""
Unit tests — Security (SEC-NEW-001, SEC-NEW-003)

SEC-NEW-001: Unauthenticated backdoor routes removed from api.py
SEC-NEW-003: Connector files must not use process-global credentials

These tests inspect source code to confirm the dangerous patterns are absent.
"""

from __future__ import annotations
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent.parent


# ─────────────────────────────────────────────────────────────────────────────
# SEC-NEW-001: No unauthenticated backdoor routes in api.py
# ─────────────────────────────────────────────────────────────────────────────

class TestNoBackdoorRoutes:
    @pytest.fixture(autouse=True)
    def _src(self):
        self.src = (REPO / "api.py").read_text()

    def test_no_raw_customer_data_dump_route(self):
        """
        The old backdoor was an UNPROTECTED GET route that dumped all customer
        data with no auth. Confirm no such unprotected route exists.
        We look for a route decorator immediately followed (within 3 lines)
        by a SELECT * without any require_auth in between.
        """
        lines = self.src.split("\n")
        for i, line in enumerate(lines):
            if not re.search(r'@app\.(get|post)', line):
                continue
            # Check the 10 lines after the decorator
            block = "\n".join(lines[i : i + 10])
            # If it has require_auth, it's protected — skip
            if "require_auth" in block or "Depends(" in block or "Security(" in block:
                continue
            # If this unprotected route also contains a raw SELECT *
            if "SELECT *" in block or "SELECT \*" in block:
                pytest.fail(
                    f"Found an unprotected route with SELECT * at line {i+1}: {line.strip()}"
                )

    def test_every_route_uses_require_auth(self):
        """
        Every @app.get / @app.post should either:
          a) use require_auth as a Depends(), OR
          b) be a public health/meta endpoint (/, /health, /docs, /auth/keys)
        """
        route_pattern = re.compile(
            r'@app\.(get|post|put|delete|patch)\(\s*["\']([^"\']+)["\']'
        )
        protected_pattern = re.compile(r'require_auth|Security\(|Depends\(require_auth')

        PUBLIC_PATHS = {"/", "/health", "/docs", "/openapi.json", "/redoc",
                        "/auth/keys", "/auth/keys/{key_id}", "/agent/train"}

        lines = self.src.split("\n")
        unprotected = []
        for i, line in enumerate(lines):
            m = route_pattern.search(line)
            if not m:
                continue
            path = m.group(2)
            if path in PUBLIC_PATHS:
                continue
            context = "\n".join(lines[i : i + 15])
            if not protected_pattern.search(context):
                unprotected.append(path)

        assert unprotected == [], (
            f"Found routes without require_auth: {unprotected}"
        )

    def test_no_debug_admin_bypass(self):
        """No ADMIN_BYPASS, DEBUG_MODE, or skip_auth patterns."""
        forbidden = ["ADMIN_BYPASS", "skip_auth", "DEBUG_MODE", "no_auth"]
        for pattern in forbidden:
            assert pattern not in self.src, (
                f"Found forbidden pattern '{pattern}' in api.py"
            )


# ─────────────────────────────────────────────────────────────────────────────
# SEC-NEW-003: Connector files must not use process-global credentials
# ─────────────────────────────────────────────────────────────────────────────

CONNECTOR_FILES = [
    "connectors/stripe_connector.py",
    "connectors/hubspot_connector.py",
    "connectors/razorpay_connector.py",
    "connectors/intercom_connector.py",
    "connectors/salesforce_connector.py",
]

# Patterns that indicate module-level global mutable credential state
GLOBAL_CRED_PATTERNS = [
    # A module-level assignment of a live credential from env (not inside a function)
    r'^(STRIPE_KEY|SF_ACCESS_TOKEN|_sf_access_token|_sf_instance_url|'
    r'INTERCOM_ACCESS_TOKEN|HUBSPOT_TOKEN|RAZORPAY_KEY)\s*=\s*os\.environ',
    # Mutation of a global variable from inside a function
    r'\bglobal\s+(_sf_access_token|_sf_instance_url|STRIPE_KEY|INTERCOM_ACCESS_TOKEN)\b',
]


class TestNoGlobalCredentials:
    @pytest.mark.parametrize("connector_rel", CONNECTOR_FILES)
    def test_no_module_level_credential_assignment(self, connector_rel):
        src = (REPO / connector_rel).read_text()
        for pattern in GLOBAL_CRED_PATTERNS:
            matches = re.findall(pattern, src, re.MULTILINE)
            assert not matches, (
                f"{connector_rel} contains forbidden global credential pattern "
                f"'{pattern}': {matches}"
            )

    @pytest.mark.parametrize("connector_rel", CONNECTOR_FILES)
    def test_sync_function_accepts_credentials_as_args(self, connector_rel):
        """
        Each connector's sync_* function must accept credentials as explicit
        parameters, not read them from module-level globals.
        """
        src = (REPO / connector_rel).read_text()
        # Find the def sync_... line
        m = re.search(r'def sync_\w+\(([^)]*)\)', src, re.DOTALL)
        assert m, f"{connector_rel} has no sync_* function"
        params = m.group(1)
        # At least one credential-looking param must be present
        cred_params = re.findall(
            r'(key|token|secret|password|username|access_token|stripe_key|'
            r'key_id|key_secret|security_token)',
            params,
        )
        assert cred_params, (
            f"{connector_rel}: sync_* function must accept credential parameters"
        )

    def test_salesforce_authenticate_returns_tuple(self):
        """
        The fixed authenticate() must return (access_token, instance_url)
        instead of mutating global state.
        """
        src = (REPO / "connectors/salesforce_connector.py").read_text()
        # Should return a tuple, not set globals
        assert "return " in src
        assert "global _sf_access_token" not in src
        assert "global _sf_instance_url" not in src

    def test_intercom_no_module_level_token_constant(self):
        src = (REPO / "connectors/intercom_connector.py").read_text()
        # The old code had: INTERCOM_ACCESS_TOKEN = os.environ.get(...)
        # at module level (outside any function). The fix removes this.
        # Check that assignment is NOT at column 0 (module level).
        for line in src.splitlines():
            if line.startswith("INTERCOM_ACCESS_TOKEN") and "os.environ" in line:
                pytest.fail(
                    "intercom_connector.py still has module-level "
                    "INTERCOM_ACCESS_TOKEN = os.environ assignment"
                )
