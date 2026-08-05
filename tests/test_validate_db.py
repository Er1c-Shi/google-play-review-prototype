"""Tests for database validation covering the live-ingestion data model."""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
DB_DIR = SRC / "db"
for path in (str(SRC), str(DB_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from ingest_live_app import ingest_live_apps  # noqa: E402
from init_db import init_db  # noqa: E402
from load_sample import ensure_app, ensure_data_source, utc_now  # noqa: E402
from validate_db import validate_db  # noqa: E402

PACKAGE_ID = "com.example.validate.app"


def _seed_app(db_path: Path) -> None:
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        now = utc_now()
        source_id = ensure_data_source(conn, now)
        ensure_app(conn, source_id, PACKAGE_ID, "Validate App", now)
        conn.commit()
    finally:
        conn.close()


def _collector(app_name: str, package_id: str, n_reviews: int) -> list[dict]:
    rows = []
    for i in range(min(n_reviews, 3)):
        rows.append(
            {
                "reviewId": f"val-{i}",
                "userName": f"u{i}",
                "userImage": "",
                "content": f"content {i}",
                "score": 5,
                "thumbsUpCount": 0,
                "reviewCreatedVersion": "1.0",
                "at": datetime(2026, 6, 1, 12, 0, i),
                "replyContent": "Thanks" if i == 0 else None,
                "repliedAt": datetime(2026, 6, 1, 13, 0, 0) if i == 0 else None,
                "appVersion": "1.0",
                "app_name": app_name,
                "app_id": package_id,
            }
        )
    return rows


def test_validate_db_passes_after_clean_live_ingest(tmp_path: Path) -> None:
    db_path = tmp_path / "validate_ok.db"
    _seed_app(db_path)
    ingest_live_apps(
        db_path=db_path,
        apps=[{"package_id": PACKAGE_ID}],
        n_reviews=3,
        collect_fn=_collector,
    )

    summary = validate_db(db_path)
    assert summary["passed"] is True
    assert summary["checks"]["no_duplicate_app_source_review_id"] == 0
    assert summary["checks"]["no_duplicate_run_observation"] == 0
    assert summary["checks"]["observation_refs_valid"] == 0
    assert summary["checks"]["app_result_counts_non_negative"] == 0
    assert summary["checks"]["completed_app_count_consistency"] == 0
    assert summary["checks"]["terminal_run_has_completed_at"] == 0
    assert summary["checks"]["no_missing_developer_reply_flags"] == 0
    assert summary["checks"]["has_developer_reply_matches_reply_content"] == 0
    assert all(item["passed"] for item in summary["check_results"])


def test_validate_db_detects_live_model_violations(tmp_path: Path) -> None:
    db_path = tmp_path / "validate_bad.db"
    _seed_app(db_path)
    ingest_live_apps(
        db_path=db_path,
        apps=[{"package_id": PACKAGE_ID}],
        n_reviews=3,
        collect_fn=_collector,
    )

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        # Break count consistency on the completed app result.
        conn.execute(
            """
            UPDATE ingestion_run_apps
            SET fetched_count = 99
            WHERE status = 'completed'
            """
        )
        # Insert a removed quality-flag type.
        processed_id = conn.execute(
            "SELECT review_processed_id FROM reviews_processed LIMIT 1"
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO review_quality_flags (
                review_processed_id, flag_type, flag_value, severity, detected_at
            )
            VALUES (?, 'missing_developer_reply', NULL, 'info', ?)
            """,
            (processed_id, utc_now()),
        )
        # Break has_developer_reply consistency.
        conn.execute(
            """
            UPDATE reviews_processed
            SET has_developer_reply = CASE has_developer_reply WHEN 1 THEN 0 ELSE 1 END
            """
        )
        # Terminal run missing completed_at.
        conn.execute(
            """
            UPDATE ingestion_runs
            SET completed_at = NULL
            WHERE status = 'completed'
            """
        )
        # Failed app without error message.
        run_id = conn.execute(
            "SELECT run_id FROM ingestion_run_apps LIMIT 1"
        ).fetchone()[0]
        now = utc_now()
        source_id = conn.execute(
            "SELECT source_id FROM data_sources LIMIT 1"
        ).fetchone()[0]
        extra_app_id = ensure_app(
            conn, source_id, "com.example.validate.extra", "Extra", now
        )
        conn.execute(
            """
            INSERT INTO ingestion_run_apps (
                run_id, app_id, status, fetched_count, inserted_count, skipped_count,
                error_message, started_at, completed_at
            )
            VALUES (?, ?, 'failed', 0, 0, 0, '', ?, ?)
            """,
            (run_id, extra_app_id, now, now),
        )
        # Running run with completed_at set (unexpected).
        conn.execute(
            """
            INSERT INTO ingestion_runs (
                source_id, started_at, completed_at, status
            )
            VALUES (?, ?, ?, 'running')
            """,
            (source_id, now, now),
        )
        conn.commit()
    finally:
        conn.close()

    summary = validate_db(db_path)
    assert summary["passed"] is False

    by_name = {item["name"]: item for item in summary["check_results"]}
    assert by_name["completed_app_count_consistency"]["passed"] is False
    assert by_name["no_missing_developer_reply_flags"]["passed"] is False
    assert by_name["has_developer_reply_matches_reply_content"]["passed"] is False
    assert by_name["terminal_run_has_completed_at"]["passed"] is False
    assert by_name["failed_app_has_error_message"]["passed"] is False
    assert by_name["running_run_state"]["passed"] is False
    assert by_name["completed_app_count_consistency"]["samples"]
