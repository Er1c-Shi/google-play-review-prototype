"""Validate relational integrity of the Google Play review SQLite database."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from init_db import DEFAULT_DB_PATH


def _count(conn: sqlite3.Connection, sql: str) -> int:
    return int(conn.execute(sql).fetchone()[0])


def validate_db(db_path: Path = DEFAULT_DB_PATH) -> dict:
    """Run integrity checks and return a summary dict."""
    db_path = Path(db_path)
    if not db_path.is_file():
        raise FileNotFoundError(f"Database not found: {db_path}")

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")

        orphan_raw_missing_app = _count(
            conn,
            """
            SELECT COUNT(*)
            FROM reviews_raw r
            LEFT JOIN apps a ON a.app_id = r.app_id
            WHERE a.app_id IS NULL
            """,
        )
        orphan_raw_missing_run = _count(
            conn,
            """
            SELECT COUNT(*)
            FROM reviews_raw r
            LEFT JOIN ingestion_runs ir ON ir.run_id = r.ingestion_run_id
            WHERE ir.run_id IS NULL
            """,
        )
        orphan_raw_reviews = orphan_raw_missing_app + orphan_raw_missing_run

        orphan_processed_reviews = _count(
            conn,
            """
            SELECT COUNT(*)
            FROM reviews_processed p
            LEFT JOIN reviews_raw r ON r.review_raw_id = p.review_raw_id
            WHERE r.review_raw_id IS NULL
            """,
        )

        orphan_quality_flags = _count(
            conn,
            """
            SELECT COUNT(*)
            FROM review_quality_flags f
            LEFT JOIN reviews_processed p
                ON p.review_processed_id = f.review_processed_id
            WHERE p.review_processed_id IS NULL
            """,
        )

        duplicate_processed_reviews = _count(
            conn,
            """
            SELECT COUNT(*) FROM (
                SELECT review_raw_id
                FROM reviews_processed
                GROUP BY review_raw_id
                HAVING COUNT(*) > 1
            )
            """,
        )

        duplicate_quality_flags = _count(
            conn,
            """
            SELECT COUNT(*) FROM (
                SELECT review_processed_id, flag_type
                FROM review_quality_flags
                GROUP BY review_processed_id, flag_type
                HAVING COUNT(*) > 1
            )
            """,
        )

        fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        foreign_key_violations = len(fk_violations)

        checks = {
            "orphan_raw_reviews": orphan_raw_reviews,
            "orphan_processed_reviews": orphan_processed_reviews,
            "orphan_quality_flags": orphan_quality_flags,
            "duplicate_processed_reviews": duplicate_processed_reviews,
            "duplicate_quality_flags": duplicate_quality_flags,
            "foreign_key_violations": foreign_key_violations,
        }
        passed = all(value == 0 for value in checks.values())

        return {
            "db_path": str(db_path.resolve()),
            "checks": checks,
            "details": {
                "orphan_raw_missing_app": orphan_raw_missing_app,
                "orphan_raw_missing_run": orphan_raw_missing_run,
            },
            "passed": passed,
        }
    finally:
        conn.close()


def print_summary(summary: dict) -> None:
    checks = summary["checks"]
    print("Database validation summary")
    print(f"  database: {summary['db_path']}")
    print()
    print("  Orphans")
    print(f"    orphan_raw_reviews:           {checks['orphan_raw_reviews']}")
    print(f"    orphan_processed_reviews:     {checks['orphan_processed_reviews']}")
    print(f"    orphan_quality_flags:         {checks['orphan_quality_flags']}")
    print()
    print("  Duplicates")
    print(
        f"    duplicate_processed_reviews:  {checks['duplicate_processed_reviews']}"
    )
    print(f"    duplicate_quality_flags:      {checks['duplicate_quality_flags']}")
    print()
    print("  Foreign keys")
    print(f"    foreign_key_violations:       {checks['foreign_key_violations']}")
    print()
    print(f"  Result: {'PASS' if summary['passed'] else 'FAIL'}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate relational integrity of the review SQLite database."
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"Path to the SQLite database file (default: {DEFAULT_DB_PATH})",
    )
    args = parser.parse_args()

    summary = validate_db(args.db_path)
    print_summary(summary)
    raise SystemExit(0 if summary["passed"] else 1)


if __name__ == "__main__":
    main()
