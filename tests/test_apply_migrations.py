"""Tests for idempotent migration application."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
DB_DIR = SRC / "db"
for path in (str(SRC), str(DB_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from apply_migrations import apply_migrations  # noqa: E402
from init_db import init_db  # noqa: E402
from validate_db import validate_db  # noqa: E402


def test_apply_migrations_bootstraps_empty_database(tmp_path: Path) -> None:
    db_path = tmp_path / "empty.db"
    summary = apply_migrations(db_path)
    assert any("bootstrapped" in a for a in summary["actions"])
    conn = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        conn.close()
    assert "review_observations" in tables
    assert "ingestion_run_apps" in tables
    assert "reviews_processed" in tables


def test_apply_migrations_upgrades_legacy_and_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    try:
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
            CREATE TABLE review_quality_flags (
                flag_id INTEGER PRIMARY KEY,
                review_processed_id INTEGER NOT NULL,
                flag_type TEXT NOT NULL,
                flag_value TEXT,
                severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'error')),
                detected_at TEXT NOT NULL,
                FOREIGN KEY (review_processed_id)
                    REFERENCES reviews_processed (review_processed_id)
            );
            """
        )
        conn.commit()
    finally:
        conn.close()

    first = apply_migrations(db_path)
    assert any(a.startswith("applied:001") for a in first["actions"])
    assert any(a.startswith("applied:002") for a in first["actions"])
    assert any(a.startswith("applied:003") for a in first["actions"])

    second = apply_migrations(db_path)
    assert any("skipped:002" in a for a in second["actions"])

    conn = sqlite3.connect(db_path)
    try:
        cols = {
            row[1] for row in conn.execute("PRAGMA table_info(reviews_processed)")
        }
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        conn.close()
    assert "has_developer_reply" in cols
    assert "review_observations" in tables
    assert "ingestion_run_apps" in tables


def test_apply_migrations_on_fresh_init_is_safe(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh.db"
    init_db(db_path)
    summary = apply_migrations(db_path)
    assert any("skipped:002" in a for a in summary["actions"])
    # Empty but fully migrated schema still validates structurally.
    # validate_db requires file with tables; no data → all violation counts 0.
    assert validate_db(db_path)["passed"] is True
