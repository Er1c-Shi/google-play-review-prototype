"""Tests for ingestion_run_apps per-app execution results."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
DB_DIR = SRC / "db"
for path in (str(SRC), str(DB_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from init_db import init_db  # noqa: E402
from load_sample import (  # noqa: E402
    finish_ingestion_run_app,
    load_review_records,
    load_sample,
    start_ingestion_run_app,
)

NOW = "2026-01-01T00:00:00+00:00"
MIGRATION = DB_DIR / "migrations" / "003_add_ingestion_run_apps.sql"


def _seed_run_and_apps(conn: sqlite3.Connection) -> tuple[int, int, int]:
    conn.execute(
        "INSERT INTO data_sources VALUES (1, 'google_play', 'Google Play', NULL, ?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO apps VALUES (1, 1, 'com.a', 'App A', NULL, ?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO apps VALUES (2, 1, 'com.b', 'App B', NULL, ?)",
        (NOW,),
    )
    conn.execute(
        """
        INSERT INTO ingestion_runs (run_id, source_id, started_at, status)
        VALUES (1, 1, ?, 'running')
        """,
        (NOW,),
    )
    return 1, 1, 2


def test_fresh_schema_includes_ingestion_run_apps(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh.db"
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "ingestion_run_apps" in tables
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(ingestion_run_apps)").fetchall()
        }
        assert {
            "id",
            "run_id",
            "app_id",
            "status",
            "fetched_count",
            "inserted_count",
            "skipped_count",
            "error_message",
            "started_at",
            "completed_at",
        } <= cols
    finally:
        conn.close()


def test_unique_run_app_constraint(tmp_path: Path) -> None:
    db_path = tmp_path / "uniq.db"
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        run_id, app_a, _app_b = _seed_run_and_apps(conn)
        start_ingestion_run_app(conn, run_id, app_a, NOW)
        with pytest.raises(sqlite3.IntegrityError):
            start_ingestion_run_app(conn, run_id, app_a, NOW)
    finally:
        conn.close()


def test_finish_marks_failed_when_no_success_and_error(tmp_path: Path) -> None:
    db_path = tmp_path / "fail.db"
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        run_id, app_a, _ = _seed_run_and_apps(conn)
        start_ingestion_run_app(conn, run_id, app_a, NOW)
        finish_ingestion_run_app(
            conn,
            run_id,
            app_a,
            fetched_count=3,
            inserted_count=0,
            skipped_count=0,
            error_message="3 review ingest errors; first: x",
            completed_at=NOW,
        )
        status, err = conn.execute(
            """
            SELECT status, error_message
            FROM ingestion_run_apps
            WHERE run_id = ? AND app_id = ?
            """,
            (run_id, app_a),
        ).fetchone()
        assert status == "failed"
        assert "ingest errors" in err
    finally:
        conn.close()


def test_finish_marks_completed_with_partial_errors(tmp_path: Path) -> None:
    db_path = tmp_path / "partial.db"
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        run_id, app_a, _ = _seed_run_and_apps(conn)
        start_ingestion_run_app(conn, run_id, app_a, NOW)
        finish_ingestion_run_app(
            conn,
            run_id,
            app_a,
            fetched_count=3,
            inserted_count=2,
            skipped_count=0,
            error_message="rev-bad: boom",
            completed_at=NOW,
        )
        status = conn.execute(
            "SELECT status FROM ingestion_run_apps WHERE run_id = ? AND app_id = ?",
            (run_id, app_a),
        ).fetchone()[0]
        assert status == "completed"
    finally:
        conn.close()


def test_loader_writes_per_app_results(tmp_path: Path) -> None:
    sample = ROOT / "data" / "samples" / "google_play_reviews_integration_sample.csv"
    db_path = tmp_path / "loader.db"
    first = load_sample(db_path=db_path, sample_path=sample)
    second = load_sample(db_path=db_path, sample_path=sample)

    assert len(first["app_results"]) == 2
    assert {app["status"] for app in first["app_results"]} == {"completed"}
    assert sum(app["fetched_count"] for app in first["app_results"]) == 400
    assert sum(app["inserted_count"] for app in first["app_results"]) == 400
    assert sum(app["skipped_count"] for app in first["app_results"]) == 0

    assert len(second["app_results"]) == 2
    assert {app["status"] for app in second["app_results"]} == {"completed"}
    assert sum(app["inserted_count"] for app in second["app_results"]) == 0
    assert sum(app["skipped_count"] for app in second["app_results"]) == 400

    conn = sqlite3.connect(db_path)
    try:
        assert (
            conn.execute("SELECT COUNT(*) FROM ingestion_run_apps").fetchone()[0] == 4
        )
        assert (
            conn.execute(
                """
                SELECT COUNT(*) FROM ingestion_run_apps
                WHERE run_id = ? AND status = 'completed'
                """,
                (first["run_id"],),
            ).fetchone()[0]
            == 2
        )
    finally:
        conn.close()


def test_migration_creates_ingestion_run_apps(tmp_path: Path) -> None:
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
                FOREIGN KEY (source_id) REFERENCES data_sources (source_id)
            );
            CREATE TABLE ingestion_runs (
                run_id INTEGER PRIMARY KEY,
                source_id INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                status TEXT NOT NULL,
                FOREIGN KEY (source_id) REFERENCES data_sources (source_id)
            );
            """
        )
        conn.executescript(MIGRATION.read_text(encoding="utf-8"))
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "ingestion_run_apps" in tables
    finally:
        conn.close()


def test_memory_loader_creates_app_result_row(tmp_path: Path) -> None:
    record = {
        "source_review_id": "mem-1",
        "package_id": "com.example.app",
        "app_name": "Example",
        "content": "ok",
        "score": 5,
        "thumbs_up_count": 0,
        "review_created_at": "2026-01-01 12:00:00",
        "app_version": "1.0",
        "reply_content": None,
        "replied_at": None,
        "raw_payload": {"reviewId": "mem-1"},
    }
    summary = load_review_records([record], db_path=tmp_path / "mem.db")
    assert summary["app_results"][0]["status"] == "completed"
    assert summary["app_results"][0]["fetched_count"] == 1
    assert summary["app_results"][0]["inserted_count"] == 1
