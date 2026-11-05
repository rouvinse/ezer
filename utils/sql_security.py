from __future__ import annotations

import re

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def ensure_safe_identifier(
    value: str,
    *,
    allowed: set[str] | None = None,
    kind: str = "identifier",
) -> str:
    """Validate SQL identifiers to prevent injection via table/column names."""
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"Unsafe SQL {kind}: {value!r}")
    if allowed is not None and value not in allowed:
        raise ValueError(f"Disallowed SQL {kind}: {value!r}")
    return value


def ensure_safe_identifiers(
    values: list[str],
    *,
    allowed: set[str] | None = None,
    kind: str = "identifier",
) -> list[str]:
    return [ensure_safe_identifier(v, allowed=allowed, kind=kind) for v in values]

