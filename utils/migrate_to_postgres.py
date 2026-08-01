"""
ACIA — SQLite → PostgreSQL Migration Script
Migrates all data from the SQLite file to a PostgreSQL database.

Usage:
  # Set your PostgreSQL connection first:
  export DB_ENGINE=postgresql
  export DATABASE_URL=postgresql://user:password@host:5432/acia
  # OR
  export DB_ENGINE=postgresql
  export DB_HOST=localhost
  export DB_NAME=acia
  export DB_USER=your_username
  export DB_PASSWORD=your_password

  # Then run:
  python utils/migrate_to_postgres.py

  # Dry run (schema only, no data):
  python utils/migrate_to_postgres.py --schema-only

  # Verify after migration:
  python utils/migrate_to_postgres.py --verify
"""

from __future__ import annotations
import sys
import sqlite3
import argparse
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

SQLITE_PATH = ROOT / "data" / "acia.db"

TABLES = [
    "customers",
    "transactions",
    "engagement_events",
    "support_tickets",
    "email_log",
    "agent_actions",
    "ml_predictions",
]


# ── Helpers ────────────────────────────────────────────────────────────────────

def get_sqlite_conn():
    if not SQLITE_PATH.exists():
        raise FileNotFoundError(
            f"SQLite database not found at {SQLITE_PATH}\n"
            f"Run 'python main.py --setup' first to generate the database."
        )
    conn = sqlite3.connect(str(SQLITE_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def get_pg_conn():
    """Get a raw psycopg2 connection for the migration."""
    try:
        import psycopg2
        import psycopg2.extras
    except ImportError:
        raise ImportError("Run: pip install psycopg2-binary")

    import os
    database_url = os.environ.get("DATABASE_URL", "")
    if database_url:
        return psycopg2.connect(database_url)

    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=int(os.environ.get("DB_PORT", "5432")),
        dbname=os.environ.get("DB_NAME", "acia"),
        user=os.environ.get("DB_USER", ""),
        password=os.environ.get("DB_PASSWORD", ""),
    )


# ── Schema creation ────────────────────────────────────────────────────────────

def create_schema(pg_conn):
    """Create all tables in PostgreSQL."""
    from utils.db import PG_SCHEMA
    cur = pg_conn.cursor()

    print("  Creating schema...")
    statements = [s.strip() for s in PG_SCHEMA.split(";") if s.strip()]
    for stmt in statements:
        try:
            cur.execute(stmt)
        except Exception as e:
            print(f"    Warning: {e}")
            pg_conn.rollback()
            cur = pg_conn.cursor()

    pg_conn.commit()
    print("  ✅ Schema created.")


# ── Data migration ─────────────────────────────────────────────────────────────

def migrate_table(table: str, sqlite_conn, pg_conn) -> int:
    """Migrate a single table from SQLite to PostgreSQL."""
    cur_sqlite = sqlite_conn.cursor()
    cur_pg     = pg_conn.cursor()

    # Get all rows from SQLite
    cur_sqlite.execute(f"SELECT * FROM {table}")
    rows = cur_sqlite.fetchall()

    if not rows:
        print(f"    {table:25s}: 0 rows (empty)")
        return 0

    # Get column names
    col_names = [d[0] for d in cur_sqlite.description]
    placeholders = ", ".join(["%s"] * len(col_names))
    cols_str     = ", ".join(col_names)

    # Clear existing data in PG (for re-runs)
    cur_pg.execute(f"DELETE FROM {table}")

    # Insert in batches of 500
    batch_size = 500
    total      = 0

    for i in range(0, len(rows), batch_size):
        batch = [tuple(row) for row in rows[i:i + batch_size]]
        cur_pg.executemany(
            f"INSERT INTO {table} ({cols_str}) VALUES ({placeholders}) "
            f"ON CONFLICT DO NOTHING",
            batch
        )
        total += len(batch)

    pg_conn.commit()
    print(f"    {table:25s}: {total:,} rows ✅")
    return total


def migrate_all_data(sqlite_conn, pg_conn) -> dict:
    """Migrate all tables."""
    print("\n  Migrating data:")
    results = {}
    for table in TABLES:
        try:
            count = migrate_table(table, sqlite_conn, pg_conn)
            results[table] = count
        except Exception as e:
            print(f"    {table:25s}: ❌ ERROR — {e}")
            pg_conn.rollback()
            results[table] = -1
    return results


# ── Verification ───────────────────────────────────────────────────────────────

def verify_migration(sqlite_conn, pg_conn):
    """Compare row counts between SQLite and PostgreSQL."""
    print("\n── Migration Verification ───────────────────────────")
    cur_sqlite = sqlite_conn.cursor()
    cur_pg     = pg_conn.cursor()

    all_match = True
    for table in TABLES:
        cur_sqlite.execute(f"SELECT COUNT(*) FROM {table}")
        sqlite_count = cur_sqlite.fetchone()[0]

        cur_pg.execute(f"SELECT COUNT(*) FROM {table}")
        pg_count = cur_pg.fetchone()[0]

        match   = sqlite_count == pg_count
        icon    = "✅" if match else "❌"
        print(f"  {icon} {table:25s}: SQLite={sqlite_count:,}  PostgreSQL={pg_count:,}")
        if not match:
            all_match = False

    print()
    if all_match:
        print("  ✅ All tables match. Migration successful.")
    else:
        print("  ❌ Row count mismatch detected. Check errors above.")

    return all_match


# ── Main ───────────────────────────────────────────────────────────────────────

def run_migration(schema_only: bool = False, verify_only: bool = False):
    import os
    engine = os.environ.get("DB_ENGINE", "sqlite")

    if engine != "postgresql":
        print("❌  DB_ENGINE is not set to 'postgresql'.")
        print("   Set it first: export DB_ENGINE=postgresql")
        print("   Then set connection details (DATABASE_URL or DB_HOST/DB_NAME/DB_USER/DB_PASSWORD)")
        sys.exit(1)

    print("=" * 54)
    print("  ACIA — SQLite → PostgreSQL Migration")
    print(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 54)

    # Connect to both
    print("\n[1/4] Connecting to SQLite...")
    try:
        sqlite_conn = get_sqlite_conn()
        cur = sqlite_conn.cursor()
        cur.execute("SELECT COUNT(*) FROM customers")
        n = cur.fetchone()[0]
        print(f"  ✅ SQLite connected ({n} customers)")
    except Exception as e:
        print(f"  ❌ SQLite connection failed: {e}")
        sys.exit(1)

    print("\n[2/4] Connecting to PostgreSQL...")
    try:
        pg_conn = get_pg_conn()
        cur = pg_conn.cursor()
        cur.execute("SELECT version()")
        version = cur.fetchone()[0].split(",")[0]
        print(f"  ✅ PostgreSQL connected ({version})")
    except Exception as e:
        print(f"  ❌ PostgreSQL connection failed: {e}")
        print("\n  Check your connection settings:")
        print("    export DATABASE_URL=postgresql://user:password@host:5432/acia")
        print("  OR")
        print("    export DB_HOST=localhost")
        print("    export DB_NAME=acia")
        print("    export DB_USER=your_username")
        print("    export DB_PASSWORD=your_password")
        sys.exit(1)

    if verify_only:
        verify_migration(sqlite_conn, pg_conn)
        sqlite_conn.close()
        pg_conn.close()
        return

    print("\n[3/4] Creating schema in PostgreSQL...")
    create_schema(pg_conn)

    if not schema_only:
        print("\n[4/4] Migrating data...")
        results = migrate_all_data(sqlite_conn, pg_conn)
        total   = sum(v for v in results.values() if v >= 0)
        errors  = sum(1 for v in results.values() if v < 0)
        print(f"\n  Total rows migrated: {total:,}")
        if errors:
            print(f"  Tables with errors:  {errors}")
    else:
        print("\n[4/4] Schema-only mode — skipping data migration.")
        results = {}

    # Verify
    print()
    if not schema_only:
        all_match = verify_migration(sqlite_conn, pg_conn)

    sqlite_conn.close()
    pg_conn.close()

    print(f"\n{'='*54}")
    if schema_only:
        print("  ✅ Schema created in PostgreSQL.")
        print("  Run without --schema-only to migrate data.")
    else:
        print("  ✅ Migration complete.")
        print()
        print("  Next steps:")
        print("  1. Set DB_ENGINE=postgresql in your environment")
        print("  2. Run: python utils/db.py  (verify connection)")
        print("  3. Run: python main.py --cycle  (test full pipeline)")
        print("  4. Your SQLite file is kept as backup at:")
        print(f"     {SQLITE_PATH}")
    print(f"{'='*54}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate ACIA from SQLite to PostgreSQL")
    parser.add_argument("--schema-only", action="store_true",
                        help="Create schema only, don't migrate data")
    parser.add_argument("--verify",      action="store_true",
                        help="Verify row counts match between SQLite and PostgreSQL")
    args = parser.parse_args()

    run_migration(schema_only=args.schema_only, verify_only=args.verify)
