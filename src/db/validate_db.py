"""Validate relational integrity of the Google Play review SQLite database.

Covers the live-ingestion data model: raw dedup, observations, per-app run
results, terminal run timestamps, quality-flag rules, and has_developer_reply.
"""

from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from init_db import DEFAULT_DB_PATH

SAMPLE_LIMIT = 5


@dataclass
class CheckResult:
    """One named validation check."""

    name: str
    description: str
    violation_count: int
    samples: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.violation_count == 0


def _count(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _sample_rows(
    conn: sqlite3.Connection,
    sql: str,
    params: Sequence[Any] = (),
    *,
    limit: int = SAMPLE_LIMIT,
) -> list[str]:
    rows = conn.execute(sql, params).fetchmany(limit)
    return [str(tuple(row)) for row in rows]


def _check(
    name: str,
    description: str,
    violation_count: int,
    samples: list[str] | None = None,
) -> CheckResult:
    return CheckResult(
        name=name,
        description=description,
        violation_count=int(violation_count),
        samples=list(samples or []),
    )


def validate_db(db_path: Path = DEFAULT_DB_PATH) -> dict:
    """Run integrity checks and return a summary dict with per-check results."""
    db_path = Path(db_path)
    if not db_path.is_file():
        raise FileNotFoundError(f"Database not found: {db_path}")

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        results: list[CheckResult] = []

        # --- Legacy / shared integrity ---
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
        results.append(
            _check(
                "orphan_raw_reviews",
                "reviews_raw rows reference existing apps and ingestion runs",
                orphan_raw_missing_app + orphan_raw_missing_run,
                _sample_rows(
                    conn,
                    """
                    SELECT r.review_raw_id, r.app_id, r.ingestion_run_id
                    FROM reviews_raw r
                    LEFT JOIN apps a ON a.app_id = r.app_id
                    LEFT JOIN ingestion_runs ir ON ir.run_id = r.ingestion_run_id
                    WHERE a.app_id IS NULL OR ir.run_id IS NULL
                    """,
                ),
            )
        )

        orphan_processed = _count(
            conn,
            """
            SELECT COUNT(*)
            FROM reviews_processed p
            LEFT JOIN reviews_raw r ON r.review_raw_id = p.review_raw_id
            WHERE r.review_raw_id IS NULL
            """,
        )
        results.append(
            _check(
                "orphan_processed_reviews",
                "reviews_processed rows reference existing reviews_raw",
                orphan_processed,
                _sample_rows(
                    conn,
                    """
                    SELECT p.review_processed_id, p.review_raw_id
                    FROM reviews_processed p
                    LEFT JOIN reviews_raw r ON r.review_raw_id = p.review_raw_id
                    WHERE r.review_raw_id IS NULL
                    """,
                ),
            )
        )

        orphan_flags = _count(
            conn,
            """
            SELECT COUNT(*)
            FROM review_quality_flags f
            LEFT JOIN reviews_processed p
                ON p.review_processed_id = f.review_processed_id
            WHERE p.review_processed_id IS NULL
            """,
        )
        results.append(
            _check(
                "orphan_quality_flags",
                "quality flags reference existing processed reviews",
                orphan_flags,
                _sample_rows(
                    conn,
                    """
                    SELECT f.flag_id, f.review_processed_id, f.flag_type
                    FROM review_quality_flags f
                    LEFT JOIN reviews_processed p
                        ON p.review_processed_id = f.review_processed_id
                    WHERE p.review_processed_id IS NULL
                    """,
                ),
            )
        )

        dup_processed = _count(
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
        results.append(
            _check(
                "duplicate_processed_reviews",
                "at most one processed row per reviews_raw",
                dup_processed,
                _sample_rows(
                    conn,
                    """
                    SELECT review_raw_id, COUNT(*) AS n
                    FROM reviews_processed
                    GROUP BY review_raw_id
                    HAVING COUNT(*) > 1
                    """,
                ),
            )
        )

        dup_flags = _count(
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
        results.append(
            _check(
                "duplicate_quality_flags",
                "at most one flag of each type per processed review",
                dup_flags,
                _sample_rows(
                    conn,
                    """
                    SELECT review_processed_id, flag_type, COUNT(*) AS n
                    FROM review_quality_flags
                    GROUP BY review_processed_id, flag_type
                    HAVING COUNT(*) > 1
                    """,
                ),
            )
        )

        fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        results.append(
            _check(
                "foreign_key_violations",
                "SQLite foreign_key_check reports no violations",
                len(fk_violations),
                [str(tuple(row)) for row in fk_violations[:SAMPLE_LIMIT]],
            )
        )

        # --- Live ingestion model ---
        dup_raw_identity = _count(
            conn,
            """
            SELECT COUNT(*) FROM (
                SELECT app_id, source_review_id
                FROM reviews_raw
                GROUP BY app_id, source_review_id
                HAVING COUNT(*) > 1
            )
            """,
        )
        results.append(
            _check(
                "no_duplicate_app_source_review_id",
                "no duplicate (app_id, source_review_id) in reviews_raw",
                dup_raw_identity,
                _sample_rows(
                    conn,
                    """
                    SELECT app_id, source_review_id, COUNT(*) AS n
                    FROM reviews_raw
                    GROUP BY app_id, source_review_id
                    HAVING COUNT(*) > 1
                    """,
                ),
            )
        )

        if _table_exists(conn, "review_observations"):
            dup_observations = _count(
                conn,
                """
                SELECT COUNT(*) FROM (
                    SELECT run_id, review_raw_id
                    FROM review_observations
                    GROUP BY run_id, review_raw_id
                    HAVING COUNT(*) > 1
                )
                """,
            )
            results.append(
                _check(
                    "no_duplicate_run_observation",
                    "no duplicate (run_id, review_raw_id) in review_observations",
                    dup_observations,
                    _sample_rows(
                        conn,
                        """
                        SELECT run_id, review_raw_id, COUNT(*) AS n
                        FROM review_observations
                        GROUP BY run_id, review_raw_id
                        HAVING COUNT(*) > 1
                        """,
                    ),
                )
            )

            orphan_obs_run = _count(
                conn,
                """
                SELECT COUNT(*)
                FROM review_observations o
                LEFT JOIN ingestion_runs ir ON ir.run_id = o.run_id
                WHERE ir.run_id IS NULL
                """,
            )
            orphan_obs_raw = _count(
                conn,
                """
                SELECT COUNT(*)
                FROM review_observations o
                LEFT JOIN reviews_raw r ON r.review_raw_id = o.review_raw_id
                WHERE r.review_raw_id IS NULL
                """,
            )
            results.append(
                _check(
                    "observation_refs_valid",
                    "each observation references a valid run and raw review",
                    orphan_obs_run + orphan_obs_raw,
                    _sample_rows(
                        conn,
                        """
                        SELECT o.id, o.run_id, o.review_raw_id
                        FROM review_observations o
                        LEFT JOIN ingestion_runs ir ON ir.run_id = o.run_id
                        LEFT JOIN reviews_raw r ON r.review_raw_id = o.review_raw_id
                        WHERE ir.run_id IS NULL OR r.review_raw_id IS NULL
                        """,
                    ),
                )
            )
        else:
            results.append(
                _check(
                    "review_observations_table",
                    "review_observations table exists",
                    1,
                    ["table review_observations is missing"],
                )
            )

        if _table_exists(conn, "ingestion_run_apps"):
            negative_counts = _count(
                conn,
                """
                SELECT COUNT(*)
                FROM ingestion_run_apps
                WHERE fetched_count < 0
                   OR inserted_count < 0
                   OR skipped_count < 0
                """,
            )
            results.append(
                _check(
                    "app_result_counts_non_negative",
                    "ingestion_run_apps counts are non-negative",
                    negative_counts,
                    _sample_rows(
                        conn,
                        """
                        SELECT id, run_id, app_id, status,
                               fetched_count, inserted_count, skipped_count
                        FROM ingestion_run_apps
                        WHERE fetched_count < 0
                           OR inserted_count < 0
                           OR skipped_count < 0
                        """,
                    ),
                )
            )

            # Normal completed apps: inserted + skipped should equal fetched.
            count_mismatch = _count(
                conn,
                """
                SELECT COUNT(*)
                FROM ingestion_run_apps
                WHERE status = 'completed'
                  AND (inserted_count + skipped_count) != fetched_count
                """,
            )
            results.append(
                _check(
                    "completed_app_count_consistency",
                    "completed app results: inserted_count + skipped_count = fetched_count",
                    count_mismatch,
                    _sample_rows(
                        conn,
                        """
                        SELECT id, run_id, app_id, fetched_count,
                               inserted_count, skipped_count,
                               (inserted_count + skipped_count) AS sum_is
                        FROM ingestion_run_apps
                        WHERE status = 'completed'
                          AND (inserted_count + skipped_count) != fetched_count
                        """,
                    ),
                )
            )

            failed_without_error = _count(
                conn,
                """
                SELECT COUNT(*)
                FROM ingestion_run_apps
                WHERE status = 'failed'
                  AND (error_message IS NULL OR TRIM(error_message) = '')
                """,
            )
            results.append(
                _check(
                    "failed_app_has_error_message",
                    "failed app results include a non-empty error_message",
                    failed_without_error,
                    _sample_rows(
                        conn,
                        """
                        SELECT id, run_id, app_id, status, error_message
                        FROM ingestion_run_apps
                        WHERE status = 'failed'
                          AND (error_message IS NULL OR TRIM(error_message) = '')
                        """,
                    ),
                )
            )

            running_app_with_completed_at = _count(
                conn,
                """
                SELECT COUNT(*)
                FROM ingestion_run_apps
                WHERE status = 'running'
                  AND completed_at IS NOT NULL
                """,
            )
            terminal_app_missing_completed_at = _count(
                conn,
                """
                SELECT COUNT(*)
                FROM ingestion_run_apps
                WHERE status IN ('completed', 'failed')
                  AND completed_at IS NULL
                """,
            )
            results.append(
                _check(
                    "running_app_result_state",
                    "running app results have no completed_at; "
                    "completed/failed app results have completed_at",
                    running_app_with_completed_at + terminal_app_missing_completed_at,
                    _sample_rows(
                        conn,
                        """
                        SELECT id, run_id, app_id, status, completed_at
                        FROM ingestion_run_apps
                        WHERE (status = 'running' AND completed_at IS NOT NULL)
                           OR (status IN ('completed', 'failed')
                               AND completed_at IS NULL)
                        """,
                    ),
                )
            )
        else:
            results.append(
                _check(
                    "ingestion_run_apps_table",
                    "ingestion_run_apps table exists",
                    1,
                    ["table ingestion_run_apps is missing"],
                )
            )

        terminal_run_missing_completed_at = _count(
            conn,
            """
            SELECT COUNT(*)
            FROM ingestion_runs
            WHERE status IN ('completed', 'partial', 'failed')
              AND completed_at IS NULL
            """,
        )
        results.append(
            _check(
                "terminal_run_has_completed_at",
                "completed/partial/failed runs have completed_at set",
                terminal_run_missing_completed_at,
                _sample_rows(
                    conn,
                    """
                    SELECT run_id, status, started_at, completed_at
                    FROM ingestion_runs
                    WHERE status IN ('completed', 'partial', 'failed')
                      AND completed_at IS NULL
                    """,
                ),
            )
        )

        running_run_bad_state = _count(
            conn,
            """
            SELECT COUNT(*)
            FROM ingestion_runs
            WHERE status = 'running'
              AND completed_at IS NOT NULL
            """,
        )
        unknown_run_status = _count(
            conn,
            """
            SELECT COUNT(*)
            FROM ingestion_runs
            WHERE status NOT IN ('running', 'completed', 'partial', 'failed')
            """,
        )
        results.append(
            _check(
                "running_run_state",
                "running runs have completed_at NULL; status is a known value",
                running_run_bad_state + unknown_run_status,
                _sample_rows(
                    conn,
                    """
                    SELECT run_id, status, completed_at
                    FROM ingestion_runs
                    WHERE (status = 'running' AND completed_at IS NOT NULL)
                       OR status NOT IN (
                            'running', 'completed', 'partial', 'failed'
                       )
                    """,
                ),
            )
        )

        missing_reply_flags = _count(
            conn,
            """
            SELECT COUNT(*)
            FROM review_quality_flags
            WHERE flag_type = 'missing_developer_reply'
            """,
        )
        results.append(
            _check(
                "no_missing_developer_reply_flags",
                "missing_developer_reply must not appear in quality flags",
                missing_reply_flags,
                _sample_rows(
                    conn,
                    """
                    SELECT flag_id, review_processed_id, flag_type, severity
                    FROM review_quality_flags
                    WHERE flag_type = 'missing_developer_reply'
                    """,
                ),
            )
        )

        if _column_exists(conn, "reviews_processed", "has_developer_reply"):
            reply_mismatch = _count(
                conn,
                """
                SELECT COUNT(*)
                FROM reviews_processed p
                JOIN reviews_raw r ON r.review_raw_id = p.review_raw_id
                WHERE (
                    p.has_developer_reply = 1
                    AND (
                        r.reply_content IS NULL
                        OR TRIM(r.reply_content) = ''
                    )
                )
                OR (
                    p.has_developer_reply = 0
                    AND r.reply_content IS NOT NULL
                    AND TRIM(r.reply_content) != ''
                )
                OR p.has_developer_reply NOT IN (0, 1)
                """,
            )
            results.append(
                _check(
                    "has_developer_reply_matches_reply_content",
                    "has_developer_reply matches non-empty trimmed reply_content",
                    reply_mismatch,
                    _sample_rows(
                        conn,
                        """
                        SELECT p.review_processed_id, p.has_developer_reply,
                               r.review_raw_id, r.reply_content
                        FROM reviews_processed p
                        JOIN reviews_raw r ON r.review_raw_id = p.review_raw_id
                        WHERE (
                            p.has_developer_reply = 1
                            AND (
                                r.reply_content IS NULL
                                OR TRIM(r.reply_content) = ''
                            )
                        )
                        OR (
                            p.has_developer_reply = 0
                            AND r.reply_content IS NOT NULL
                            AND TRIM(r.reply_content) != ''
                        )
                        OR p.has_developer_reply NOT IN (0, 1)
                        """,
                    ),
                )
            )
        else:
            results.append(
                _check(
                    "has_developer_reply_column",
                    "reviews_processed.has_developer_reply column exists",
                    1,
                    ["column has_developer_reply is missing"],
                )
            )

        checks = {item.name: item.violation_count for item in results}
        passed = all(item.passed for item in results)

        return {
            "db_path": str(db_path.resolve()),
            "checks": checks,
            "check_results": [
                {
                    "name": item.name,
                    "description": item.description,
                    "violation_count": item.violation_count,
                    "passed": item.passed,
                    "samples": item.samples,
                }
                for item in results
            ],
            "details": {
                "orphan_raw_missing_app": orphan_raw_missing_app,
                "orphan_raw_missing_run": orphan_raw_missing_run,
            },
            "passed": passed,
        }
    finally:
        conn.close()


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in rows)


def print_summary(summary: dict) -> None:
    print("Database validation summary")
    print(f"  database: {summary['db_path']}")
    print()

    for item in summary["check_results"]:
        status = "PASS" if item["passed"] else "FAIL"
        count = item["violation_count"]
        print(f"  [{status}] {item['name']}  (violations={count})")
        print(f"         {item['description']}")
        if not item["passed"]:
            print("         issues:")
            if item["samples"]:
                for sample in item["samples"]:
                    print(f"           - {sample}")
            else:
                print(f"           - {count} violation(s) (no sample rows)")
        print()

    print(f"  Result: {'PASS' if summary['passed'] else 'FAIL'}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Validate relational integrity of the review SQLite database, "
            "including live-ingestion observations and per-app run results."
        )
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
