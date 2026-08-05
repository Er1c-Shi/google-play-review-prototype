"""Integration tests for single-app live ingestion → SQLite."""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
DB_DIR = SRC / "db"
for path in (str(SRC), str(DB_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from init_db import init_db  # noqa: E402
from ingest_live_app import ingest_live_app  # noqa: E402
from load_sample import create_ingestion_run, ensure_app, ensure_data_source, utc_now  # noqa: E402


PACKAGE_ID = "com.example.liveapp"
APP_NAME = "Live Example"


def _seed_existing_app(db_path: Path) -> int:
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        now = utc_now()
        source_id = ensure_data_source(conn, now)
        app_id = ensure_app(conn, source_id, PACKAGE_ID, APP_NAME, now)
        conn.commit()
        return app_id
    finally:
        conn.close()


def _fake_live_reviews(app_name: str, package_id: str, n_reviews: int) -> list[dict]:
    rows = []
    for i in range(n_reviews):
        rows.append(
            {
                "reviewId": f"live-rev-{i}",
                "userName": f"user-{i}",
                "userImage": "",
                "content": f"Review body {i}",
                "score": 4,
                "thumbsUpCount": i,
                "reviewCreatedVersion": "1.0.0",
                "at": datetime(2026, 1, 1, 12, 0, i % 60),
                "replyContent": "Thanks!" if i % 2 == 0 else None,
                "repliedAt": datetime(2026, 1, 2, 8, 0, 0) if i % 2 == 0 else None,
                "appVersion": "1.0.0",
                "app_name": app_name,
                "app_id": package_id,
            }
        )
    return rows


def test_live_ingest_single_app_inserts_and_observes(tmp_path: Path) -> None:
    db_path = tmp_path / "live.db"
    app_id = _seed_existing_app(db_path)

    summary = ingest_live_app(
        db_path=db_path,
        app_id=app_id,
        n_reviews=5,
        collect_fn=_fake_live_reviews,
    )

    assert summary["status"] == "completed"
    assert summary["fetched_count"] == 5
    assert summary["inserted_count"] == 5
    assert summary["skipped_count"] == 0
    assert summary["error_message"] is None
    assert summary["completed_at"] is not None

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM reviews_raw").fetchone()[0] == 5
        assert (
            conn.execute("SELECT COUNT(*) FROM review_observations").fetchone()[0] == 5
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM reviews_processed").fetchone()[0] == 5
        )
        app_row = conn.execute(
            """
            SELECT status, fetched_count, inserted_count, skipped_count
            FROM ingestion_run_apps
            WHERE run_id = ? AND app_id = ?
            """,
            (summary["run_id"], app_id),
        ).fetchone()
        assert app_row == ("completed", 5, 5, 0)
        run = conn.execute(
            """
            SELECT status, total_fetched, total_inserted, skipped_duplicates
            FROM ingestion_runs WHERE run_id = ?
            """,
            (summary["run_id"],),
        ).fetchone()
        assert run[0] == "completed"
        assert run[1:] == (5, 5, 0)
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM ingestion_run_apps WHERE status = 'running'"
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_live_ingest_second_run_skips_duplicates_but_adds_observations(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "live2.db"
    _seed_existing_app(db_path)

    first = ingest_live_app(
        db_path=db_path,
        package_id=PACKAGE_ID,
        n_reviews=3,
        collect_fn=_fake_live_reviews,
    )
    second = ingest_live_app(
        db_path=db_path,
        package_id=PACKAGE_ID,
        n_reviews=3,
        collect_fn=_fake_live_reviews,
    )

    assert first["inserted_count"] == 3
    assert second["inserted_count"] == 0
    assert second["skipped_count"] == 3
    assert second["fetched_count"] == 3
    assert second["status"] == "completed"
    assert second["run_id"] != first["run_id"]

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM reviews_raw").fetchone()[0] == 3
        assert (
            conn.execute("SELECT COUNT(*) FROM review_observations").fetchone()[0] == 6
        )
    finally:
        conn.close()


def test_live_ingest_collector_failure_marks_failed_and_reraises(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "live_fail.db"
    app_id = _seed_existing_app(db_path)

    def boom(app_name: str, package_id: str, n_reviews: int) -> list[dict]:
        raise RuntimeError("google play unavailable")

    with pytest.raises(RuntimeError, match="google play unavailable"):
        ingest_live_app(
            db_path=db_path,
            app_id=app_id,
            n_reviews=2,
            collect_fn=boom,
        )

    conn = sqlite3.connect(db_path)
    try:
        run = conn.execute(
            "SELECT run_id, status FROM ingestion_runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        assert run is not None
        assert run[1] == "failed"
        app_result = conn.execute(
            """
            SELECT status, error_message, completed_at, fetched_count
            FROM ingestion_run_apps
            WHERE run_id = ? AND app_id = ?
            """,
            (run[0], app_id),
        ).fetchone()
        assert app_result[0] == "failed"
        assert "google play unavailable" in app_result[1]
        assert app_result[2] is not None
        assert app_result[3] == 0
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM ingestion_run_apps WHERE status = 'running'"
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_live_ingest_requires_existing_app(tmp_path: Path) -> None:
    db_path = tmp_path / "missing.db"
    init_db(db_path)
    with pytest.raises(LookupError, match="No app"):
        ingest_live_app(
            db_path=db_path,
            package_id="com.does.not.exist",
            n_reviews=1,
            collect_fn=_fake_live_reviews,
        )


def test_live_ingest_reuses_provided_run_id(tmp_path: Path) -> None:
    db_path = tmp_path / "reuse_run.db"
    app_id = _seed_existing_app(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        now = utc_now()
        source_id = conn.execute(
            "SELECT source_id FROM data_sources LIMIT 1"
        ).fetchone()[0]
        run_id = create_ingestion_run(
            conn,
            source_id=source_id,
            started_at=now,
            app_count=1,
            target_review_count=2,
            notes="precreated run",
        )
        conn.commit()
    finally:
        conn.close()

    summary = ingest_live_app(
        db_path=db_path,
        app_id=app_id,
        run_id=run_id,
        n_reviews=2,
        collect_fn=_fake_live_reviews,
    )
    assert summary["run_id"] == run_id
    assert summary["inserted_count"] == 2
