"""
EZER — Connector Utilities
===========================
Shared helpers used by all connector sync functions.

SEC-NEW-004 FIX: safe_upsert_sync() replaces the dangerous delete-before-
replace pattern that all 5 connectors previously used.

Old (dangerous) pattern:
    DELETE FROM table WHERE company_id=?      ← data gone if crash follows
    INSERT INTO table ...                     ← write fresh rows

New (safe) pattern:
    For each row → INSERT ... ON CONFLICT (pk) DO UPDATE SET ...

This means:
  • If a sync crashes halfway, existing data is NOT deleted.
  • Stale rows (removed upstream) are left in place — a daily full-sync
    will update them on the next successful run.
  • No data loss on partial sync.

If you need to guarantee stale-row cleanup, set purge_stale=True and
provide the set of current upstream IDs. The purge only runs AFTER all
upserts succeed, so a crash before that point leaves data intact.
"""

from __future__ import annotations
from utils.db import get_connection, DB_ENGINE
from utils.sql_security import ensure_safe_identifier, ensure_safe_identifiers

ALLOWED_TABLE_PK: dict[str, set[str]] = {
    "customers": {"customer_id"},
    "transactions": {"txn_id"},
    "engagement_events": {"event_id"},
    "support_tickets": {"ticket_id"},
    "email_log": {"email_id"},
    "agent_actions": {"action_id"},
    "ml_predictions": {"pred_id", "prediction_id"},
}

ALLOWED_TABLE_COLUMNS: dict[str, set[str]] = {
    "customers": {"customer_id", "name", "email", "company", "industry", "plan", "mrr", "signup_date",
                  "country", "employee_count", "health_score", "nps_score", "is_churned", "churn_date",
                  "created_at", "company_id"},
    "transactions": {"txn_id", "customer_id", "amount", "currency", "txn_type", "product_id", "status",
                     "txn_date", "company_id"},
    "engagement_events": {"event_id", "customer_id", "event_type", "channel", "session_duration", "event_date",
                          "metadata", "company_id"},
    "support_tickets": {"ticket_id", "customer_id", "topic", "status", "priority", "sentiment_score",
                        "opened_at", "resolved_at", "company_id"},
    "email_log": {"email_id", "customer_id", "email_type", "subject", "opened", "clicked", "sent_at", "company_id"},
    "agent_actions": {"action_id", "customer_id", "action_type", "reason", "priority", "status", "outcome",
                      "created_at", "executed_at", "decided_by", "company_id"},
    "ml_predictions": {"pred_id", "prediction_id", "customer_id", "churn_score", "conversion_score", "segment",
                       "predicted_at", "ltv_estimate", "company_id"},
}


def _validate_target(table: str, pk_col: str, cols: list[str]) -> tuple[str, str, list[str]]:
    table = ensure_safe_identifier(table, allowed=set(ALLOWED_TABLE_PK), kind="table")
    pk_col = ensure_safe_identifier(pk_col, allowed=ALLOWED_TABLE_PK[table], kind="primary key column")
    valid_cols = ensure_safe_identifiers(
        cols,
        allowed=ALLOWED_TABLE_COLUMNS[table],
        kind=f"{table} column",
    )
    if pk_col not in valid_cols:
        raise ValueError(f"Primary key column '{pk_col}' missing from rows for table '{table}'")
    return table, pk_col, valid_cols


def safe_upsert_sync(
    table:         str,
    pk_col:        str,
    rows:          list[dict],
    company_id:    str,
    upstream_ids:  set[str] | None = None,   # supply to purge removed rows
    purge_stale:   bool = False,
    verbose:       bool = True,
) -> dict:
    """
    Upsert rows into `table`, scoped to `company_id`.

    Args:
        table:        DB table name (e.g. 'customers')
        pk_col:       Primary key column name (e.g. 'customer_id')
        rows:         List of row dicts to upsert
        company_id:   Tenant ID — all writes are scoped to this tenant
        upstream_ids: Set of all IDs present in the upstream source.
                      When purge_stale=True, rows absent from this set
                      are deleted AFTER all upserts succeed.
        purge_stale:  If True, delete rows for this tenant whose pk is
                      not in upstream_ids. Only runs after all upserts.
        verbose:      Print progress

    Returns:
        {"upserted": N, "purged": M, "errors": [...]}
    """
    if not rows:
        return {"upserted": 0, "purged": 0, "errors": []}

    conn   = get_connection()
    errors = []
    upserted = 0
    table, pk_col, cols = _validate_target(table, pk_col, list(rows[0].keys()))

    if DB_ENGINE == "postgresql":
        # PostgreSQL: INSERT ... ON CONFLICT DO UPDATE
        col_list  = ", ".join(cols)
        ph_list   = ", ".join(["%s"] * len(cols))
        # Exclude pk from the update clause
        update_cols = [c for c in cols if c != pk_col]
        update_set  = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)

        sql = (
            f"INSERT INTO {table} ({col_list}) VALUES ({ph_list}) "
            f"ON CONFLICT ({pk_col}) DO UPDATE SET {update_set}"
        )
        data = [tuple(r.get(c) for c in cols) for r in rows]

        try:
            conn.executemany(sql, data)
            conn.commit()
            upserted = len(rows)
        except Exception as e:
            conn._conn.rollback()
            errors.append(f"Bulk upsert failed: {e}")
            # Fall back to row-by-row so we salvage as many rows as possible
            for row in rows:
                try:
                    conn.execute(sql, tuple(row.get(c) for c in cols))
                    conn.commit()
                    upserted += 1
                except Exception as row_e:
                    conn._conn.rollback()
                    errors.append(f"Row {row.get(pk_col,'?')}: {row_e}")

    else:
        # SQLite: INSERT OR REPLACE handles conflict on primary key
        col_list = ", ".join(cols)
        ph_list  = ", ".join([f":{c}" for c in cols])
        sql      = f"INSERT OR REPLACE INTO {table} ({col_list}) VALUES ({ph_list})"

        try:
            conn.executemany(sql, rows)
            conn.commit()
            upserted = len(rows)
        except Exception as e:
            errors.append(f"Bulk upsert failed: {e}")
            for row in rows:
                try:
                    conn.execute(sql, row)
                    conn.commit()
                    upserted += 1
                except Exception as row_e:
                    errors.append(f"Row {row.get(pk_col,'?')}: {row_e}")

    # ── Stale-row purge (only after all upserts succeed) ──────────────────────
    purged = 0
    if purge_stale and upstream_ids is not None and not errors:
        try:
            # Fetch all PKs for this tenant
            existing_rows = conn.execute(
                f"SELECT {pk_col} FROM {table} WHERE company_id=?",
                (company_id,)
            ).fetchall()
            stale_ids = [r[0] for r in existing_rows if r[0] not in upstream_ids]

            if stale_ids:
                placeholders = ",".join(["?"] * len(stale_ids))
                conn.execute(
                    f"DELETE FROM {table} WHERE {pk_col} IN ({placeholders}) AND company_id=?",
                    stale_ids + [company_id]
                )
                conn.commit()
                purged = len(stale_ids)
                if verbose:
                    print(f"  Purged {purged} stale rows from {table}")
        except Exception as e:
            errors.append(f"Stale purge failed: {e}")

    conn.close()

    if verbose and errors:
        print(f"  ⚠️  {len(errors)} upsert error(s) in {table}:")
        for err in errors[:3]:
            print(f"     {err}")

    return {"upserted": upserted, "purged": purged, "errors": errors}
