"""Apply incremental SQL migrations to an existing (or empty) SQLite database.

Empty databases are bootstrapped via ``init_db`` (full ``schema.sql``).
Legacy Version 2 databases receive ``001``–``003`` in order; ``002`` is skipped
when ``has_developer_reply`` already exists (SQLite cannot ``ADD COLUMN IF NOT EXISTS``).
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from init_db import DEFAULT_DB_PATH, init_db

MIGRATIONS_DIR = Path(__file__).with_name("migrations")
MIGRATION_FILES = (
    "001_add_review_observations.sql",
    "002_add_has_developer_reply.sql",
    "003_add_ingestion_run_apps.sql",
)


def _user_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
        """
    ).fetchall()
    return {row[0] for row in rows}


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    if table not in _user_tables(conn):
        return False
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in rows)


def apply_migrations(db_path: Path = DEFAULT_DB_PATH) -> dict:
    """
    Bring ``db_path`` up to the current schema.

    Returns a summary of actions taken.
    """
    db_path = Path(db_path)
    actions: list[str] = []

    if not db_path.is_file() or db_path.stat().st_size == 0:
        init_db(db_path)
        actions.append("bootstrapped_via_init_db")
        return {"db_path": str(db_path.resolve()), "actions": actions}

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        tables = _user_tables(conn)
        if not tables:
            conn.close()
            init_db(db_path)
            actions.append("bootstrapped_via_init_db")
            return {"db_path": str(db_path.resolve()), "actions": actions}

        for name in MIGRATION_FILES:
            path = MIGRATIONS_DIR / name
            if not path.is_file():
                raise FileNotFoundError(f"Missing migration: {path}")

            if name.startswith("002_") and _column_exists(
                conn, "reviews_processed", "has_developer_reply"
            ):
                actions.append(f"skipped:{name} (column already present)")
                continue

            if name.startswith("002_") and "reviews_processed" not in tables:
                raise RuntimeError(
                    "Migration 002 requires reviews_processed. "
                    "Use init_db.py for a new database, or restore a Version 2 schema first."
                )

            conn.executescript(path.read_text(encoding="utf-8"))
            conn.commit()
            tables = _user_tables(conn)
            actions.append(f"applied:{name}")
    finally:
        conn.close()

    return {"db_path": str(db_path.resolve()), "actions": actions}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apply SQLite schema migrations (or bootstrap via init_db)."
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"SQLite database path (default: {DEFAULT_DB_PATH})",
    )
    args = parser.parse_args()
    summary = apply_migrations(args.db_path)
    print(f"Migrations finished for: {summary['db_path']}")
    for action in summary["actions"]:
        print(f"  - {action}")


if __name__ == "__main__":
    main()
