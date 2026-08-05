"""Shared fixtures for SQLite schema tests."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DB_DIR = ROOT / "src" / "db"
if str(DB_DIR) not in sys.path:
    sys.path.insert(0, str(DB_DIR))

from init_db import init_db  # noqa: E402

NOW = "2026-01-01T00:00:00+00:00"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Create an empty Version 2 SQLite database from schema.sql."""
    path = tmp_path / "test_google_play_reviews.db"
    init_db(path)
    return path


@pytest.fixture
def conn(db_path: Path):
    """Open a connection with foreign keys enabled (project default)."""
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


@pytest.fixture
def seeded(conn: sqlite3.Connection) -> dict[str, int]:
    """
    Seed minimal parent rows used by review_observations tests:
    one data source, one app, two ingestion runs, one reviews_raw row.
    """
    conn.execute(
        """
        INSERT INTO data_sources (source_id, source_code, source_name, description, created_at)
        VALUES (1, 'google_play', 'Google Play', 'test source', ?)
        """,
        (NOW,),
    )
    conn.execute(
        """
        INSERT INTO apps (app_id, source_id, source_app_identifier, app_name, metadata_json, created_at)
        VALUES (1, 1, 'com.example.app', 'Example App', NULL, ?)
        """,
        (NOW,),
    )
    for run_id in (1, 2):
        conn.execute(
            """
            INSERT INTO ingestion_runs (
                run_id, source_id, started_at, completed_at, status,
                sort_order, country, language, target_review_count, app_count
            )
            VALUES (?, 1, ?, ?, 'completed', 'newest', 'us', 'en', 10, 1)
            """,
            (run_id, NOW, NOW),
        )
    conn.execute(
        """
        INSERT INTO reviews_raw (
            review_raw_id,
            ingestion_run_id,
            app_id,
            source_review_id,
            content,
            score,
            thumbs_up_count,
            review_created_at,
            app_version,
            reply_content,
            replied_at,
            collected_at,
            raw_payload_json
        )
        VALUES (1, 1, 1, 'src-review-001', 'Great app', 5, 0, ?, '1.0.0', NULL, NULL, ?, '{}')
        """,
        (NOW, NOW),
    )
    conn.commit()
    return {
        "source_id": 1,
        "app_id": 1,
        "run_id_1": 1,
        "run_id_2": 2,
        "review_raw_id": 1,
    }
