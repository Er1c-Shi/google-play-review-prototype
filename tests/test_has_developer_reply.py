"""Migration / schema tests for has_developer_reply availability feature."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_DIR = ROOT / "src" / "db"
if str(DB_DIR) not in sys.path:
    sys.path.insert(0, str(DB_DIR))

from init_db import init_db  # noqa: E402

NOW = "2026-01-01T00:00:00+00:00"
MIGRATION = DB_DIR / "migrations" / "002_add_has_developer_reply.sql"


def _legacy_schema_without_has_developer_reply(conn: sqlite3.Connection) -> None:
    """Create a minimal pre-migration schema for backfill testing."""
    conn.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE data_sources (
            source_id INTEGER PRIMARY KEY,
            source_code TEXT NOT NULL UNIQUE,
            source_name TEXT NOT NULL,
            description TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE apps (
            app_id INTEGER PRIMARY KEY,
            source_id INTEGER NOT NULL,
            source_app_identifier TEXT NOT NULL,
            app_name TEXT NOT NULL,
            metadata_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (source_id) REFERENCES data_sources (source_id),
            UNIQUE (source_id, source_app_identifier)
        );
        CREATE TABLE ingestion_runs (
            run_id INTEGER PRIMARY KEY,
            source_id INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            status TEXT NOT NULL,
            FOREIGN KEY (source_id) REFERENCES data_sources (source_id)
        );
        CREATE TABLE reviews_raw (
            review_raw_id INTEGER PRIMARY KEY,
            ingestion_run_id INTEGER NOT NULL,
            app_id INTEGER NOT NULL,
            source_review_id TEXT NOT NULL,
            content TEXT,
            score INTEGER,
            thumbs_up_count INTEGER,
            review_created_at TEXT,
            app_version TEXT,
            reply_content TEXT,
            replied_at TEXT,
            collected_at TEXT NOT NULL,
            raw_payload_json TEXT NOT NULL,
            FOREIGN KEY (ingestion_run_id) REFERENCES ingestion_runs (run_id),
            FOREIGN KEY (app_id) REFERENCES apps (app_id),
            UNIQUE (app_id, source_review_id)
        );
        CREATE TABLE reviews_processed (
            review_processed_id INTEGER PRIMARY KEY,
            review_raw_id INTEGER NOT NULL UNIQUE,
            cleaned_content TEXT,
            normalized_score INTEGER,
            text_length INTEGER,
            language_code TEXT,
            processed_at TEXT NOT NULL,
            processing_version TEXT NOT NULL,
            FOREIGN KEY (review_raw_id) REFERENCES reviews_raw (review_raw_id)
        );
        """
    )


def test_migration_backfills_has_developer_reply(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        _legacy_schema_without_has_developer_reply(conn)
        conn.execute(
            "INSERT INTO data_sources VALUES (1, 'google_play', 'Google Play', NULL, ?)",
            (NOW,),
        )
        conn.execute(
            "INSERT INTO apps VALUES (1, 1, 'com.example.app', 'Example', NULL, ?)",
            (NOW,),
        )
        conn.execute(
            "INSERT INTO ingestion_runs (run_id, source_id, started_at, status) VALUES (1, 1, ?, 'completed')",
            (NOW,),
        )
        conn.execute(
            """
            INSERT INTO reviews_raw (
                review_raw_id, ingestion_run_id, app_id, source_review_id,
                content, score, collected_at, raw_payload_json, reply_content
            ) VALUES
                (1, 1, 1, 'a', 'hi', 5, ?, '{}', 'Thanks!'),
                (2, 1, 1, 'b', 'hi', 4, ?, '{}', NULL),
                (3, 1, 1, 'c', 'hi', 3, ?, '{}', '   ')
            """,
            (NOW, NOW, NOW),
        )
        conn.execute(
            """
            INSERT INTO reviews_processed (
                review_processed_id, review_raw_id, cleaned_content,
                normalized_score, text_length, language_code,
                processed_at, processing_version
            ) VALUES
                (1, 1, 'hi', 5, 2, NULL, ?, 'v1-basic'),
                (2, 2, 'hi', 4, 2, NULL, ?, 'v1-basic'),
                (3, 3, 'hi', 3, 2, NULL, ?, 'v1-basic')
            """,
            (NOW, NOW, NOW),
        )
        conn.commit()

        conn.executescript(MIGRATION.read_text(encoding="utf-8"))
        conn.commit()

        rows = {
            int(review_raw_id): int(has_reply)
            for review_raw_id, has_reply in conn.execute(
                """
                SELECT review_raw_id, has_developer_reply
                FROM reviews_processed
                ORDER BY review_raw_id
                """
            )
        }
        assert rows == {1: 1, 2: 0, 3: 0}
    finally:
        conn.close()


def test_fresh_schema_includes_has_developer_reply(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh.db"
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(reviews_processed)").fetchall()
        }
        assert "has_developer_reply" in cols
    finally:
        conn.close()
