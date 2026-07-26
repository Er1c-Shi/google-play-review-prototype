"""Initialize the SQLite database from the Version 2 schema."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
DEFAULT_DB_PATH = Path("data") / "google_play_reviews.db"


def init_db(db_path: Path = DEFAULT_DB_PATH) -> Path:
    """Create the database file and apply schema.sql with foreign keys enabled."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(schema_sql)
        conn.commit()

        fk_enabled = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        if not fk_enabled:
            raise RuntimeError("SQLite foreign keys are not enabled after initialization.")

    return db_path.resolve()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Initialize the Google Play review SQLite database (Version 2 schema)."
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"Path to the SQLite database file (default: {DEFAULT_DB_PATH})",
    )
    args = parser.parse_args()

    resolved = init_db(args.db_path)
    print(f"Database initialized at: {resolved}")


if __name__ == "__main__":
    main()
