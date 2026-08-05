"""
Immediate repeat collection scenario: same fixed review set ingested twice in a row.

Collector is mocked; pipeline and SQLite writes are real.
"""

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

APP_A = ("com.example.repeat.a", "Repeat A")
APP_B = ("com.example.repeat.b", "Repeat B")

FIXED_REVIEWS: dict[str, list[dict]] = {
    APP_A[0]: [
        {
            "reviewId": "repeat-a-1",
            "userName": "u1",
            "userImage": "",
            "content": "Love it",
            "score": 5,
            "thumbsUpCount": 1,
            "reviewCreatedVersion": "1.0.0",
            "at": datetime(2026, 4, 1, 8, 0, 0),
            "replyContent": "Thanks",
            "repliedAt": datetime(2026, 4, 1, 9, 0, 0),
            "appVersion": "1.0.0",
        },
        {
            "reviewId": "repeat-a-2",
            "userName": "u2",
            "userImage": "",
            "content": "Okay",
            "score": 3,
            "thumbsUpCount": 0,
            "reviewCreatedVersion": "1.0.0",
            "at": datetime(2026, 4, 1, 8, 5, 0),
            "replyContent": None,
            "repliedAt": None,
            "appVersion": "1.0.0",
        },
        {
            "reviewId": "repeat-a-3",
            "userName": "u3",
            "userImage": "",
            "content": "Bugs",
            "score": 2,
            "thumbsUpCount": 0,
            "reviewCreatedVersion": "1.0.0",
            "at": datetime(2026, 4, 1, 8, 10, 0),
            "replyContent": None,
            "repliedAt": None,
            "appVersion": "1.0.0",
        },
    ],
    APP_B[0]: [
        {
            "reviewId": "repeat-b-1",
            "userName": "u4",
            "userImage": "",
            "content": "Fast",
            "score": 5,
            "thumbsUpCount": 2,
            "reviewCreatedVersion": "2.0.0",
            "at": datetime(2026, 4, 1, 10, 0, 0),
            "replyContent": None,
            "repliedAt": None,
            "appVersion": "2.0.0",
        },
        {
            "reviewId": "repeat-b-2",
            "userName": "u5",
            "userImage": "",
            "content": "Slow",
            "score": 1,
            "thumbsUpCount": 0,
            "reviewCreatedVersion": "2.0.0",
            "at": datetime(2026, 4, 1, 10, 5, 0),
            "replyContent": None,
            "repliedAt": None,
            "appVersion": "2.0.0",
        },
    ],
}

EXPECTED_RAW = sum(len(rows) for rows in FIXED_REVIEWS.values())  # 5
APPS = [{"package_id": APP_A[0]}, {"package_id": APP_B[0]}]


def _prepare_db(db_path: Path) -> None:
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        now = utc_now()
        source_id = ensure_data_source(conn, now)
        for package_id, app_name in (APP_A, APP_B):
            ensure_app(conn, source_id, package_id, app_name, now)
        conn.commit()
    finally:
        conn.close()


def _mock_collector(app_name: str, package_id: str, n_reviews: int) -> list[dict]:
    rows = FIXED_REVIEWS[package_id]
    selected = rows[:n_reviews] if n_reviews < len(rows) else list(rows)
    out = []
    for row in selected:
        item = dict(row)
        item["app_name"] = app_name
        item["app_id"] = package_id
        out.append(item)
    return out


def _run_stats(conn: sqlite3.Connection, run_id: int) -> dict:
    run = conn.execute(
        """
        SELECT status, total_fetched, total_inserted, skipped_duplicates, completed_at
        FROM ingestion_runs
        WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()
    app_rows = conn.execute(
        """
        SELECT a.source_app_identifier, r.status, r.fetched_count,
               r.inserted_count, r.skipped_count
        FROM ingestion_run_apps r
        JOIN apps a ON a.app_id = r.app_id
        WHERE r.run_id = ?
        ORDER BY a.source_app_identifier
        """,
        (run_id,),
    ).fetchall()
    obs_count = conn.execute(
        "SELECT COUNT(*) FROM review_observations WHERE run_id = ?",
        (run_id,),
    ).fetchone()[0]
    obs_dupes = conn.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT run_id, review_raw_id
            FROM review_observations
            WHERE run_id = ?
            GROUP BY run_id, review_raw_id
            HAVING COUNT(*) > 1
        )
        """,
        (run_id,),
    ).fetchone()[0]
    return {
        "run_id": run_id,
        "status": run[0],
        "total_fetched": int(run[1] or 0),
        "total_inserted": int(run[2] or 0),
        "skipped_duplicates": int(run[3] or 0),
        "completed_at": run[4],
        "app_results": [
            {
                "package_id": row[0],
                "status": row[1],
                "fetched_count": int(row[2]),
                "inserted_count": int(row[3]),
                "skipped_count": int(row[4]),
            }
            for row in app_rows
        ],
        "observations": int(obs_count),
        "observation_dupes_in_run": int(obs_dupes),
    }


def test_immediate_repeat_collection_same_review_set(tmp_path: Path) -> None:
    db_path = tmp_path / "immediate_repeat.db"
    _prepare_db(db_path)

    first = ingest_live_apps(
        db_path=db_path,
        apps=APPS,
        n_reviews=10,
        collect_fn=_mock_collector,
    )
    second = ingest_live_apps(
        db_path=db_path,
        apps=APPS,
        n_reviews=10,
        collect_fn=_mock_collector,
    )

    assert first["status"] == "completed"
    assert second["status"] == "completed"
    assert first["run_id"] != second["run_id"]
    assert first["apps_completed"] == 2
    assert second["apps_completed"] == 2
    assert first["apps_failed"] == 0
    assert second["apps_failed"] == 0

    conn = sqlite3.connect(db_path)
    try:
        run_count = conn.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0]
        assert run_count == 2

        stats1 = _run_stats(conn, first["run_id"])
        stats2 = _run_stats(conn, second["run_id"])

        assert stats1["status"] == "completed"
        assert stats2["status"] == "completed"
        assert stats1["completed_at"] is not None
        assert stats2["completed_at"] is not None

        # First run inserts everything; second skips all as duplicates.
        assert stats1["total_fetched"] == EXPECTED_RAW
        assert stats1["total_inserted"] == EXPECTED_RAW
        assert stats1["skipped_duplicates"] == 0

        assert stats2["total_fetched"] == EXPECTED_RAW
        assert stats2["total_inserted"] == 0
        assert stats2["skipped_duplicates"] == EXPECTED_RAW
        assert stats2["skipped_duplicates"] == stats2["total_fetched"]

        for app in stats1["app_results"]:
            assert app["status"] == "completed"
            assert app["inserted_count"] == app["fetched_count"]
            assert app["skipped_count"] == 0
        for app in stats2["app_results"]:
            assert app["status"] == "completed"
            assert app["inserted_count"] == 0
            assert app["skipped_count"] == app["fetched_count"]

        raw_count = conn.execute("SELECT COUNT(*) FROM reviews_raw").fetchone()[0]
        assert raw_count == EXPECTED_RAW  # unchanged after second run

        # Each run has its own observations; no within-run duplicates.
        assert stats1["observations"] == EXPECTED_RAW
        assert stats2["observations"] == EXPECTED_RAW
        assert stats1["observation_dupes_in_run"] == 0
        assert stats2["observation_dupes_in_run"] == 0

        total_obs = conn.execute(
            "SELECT COUNT(*) FROM review_observations"
        ).fetchone()[0]
        assert total_obs == EXPECTED_RAW * 2

        # Junction uniqueness: no duplicate (run_id, review_raw_id) anywhere.
        junction_dupes = conn.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT run_id, review_raw_id
                FROM review_observations
                GROUP BY run_id, review_raw_id
                HAVING COUNT(*) > 1
            )
            """
        ).fetchone()[0]
        assert junction_dupes == 0

        # Processed rows created once on first insert; not recreated on skip.
        processed_count = conn.execute(
            "SELECT COUNT(*) FROM reviews_processed"
        ).fetchone()[0]
        assert processed_count == EXPECTED_RAW

        processed_per_raw = conn.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT review_raw_id
                FROM reviews_processed
                GROUP BY review_raw_id
                HAVING COUNT(*) > 1
            )
            """
        ).fetchone()[0]
        assert processed_per_raw == 0

        print("\nImmediate repeat collection — run comparison")
        print(
            f"  run1: id={stats1['run_id']} status={stats1['status']} "
            f"fetched={stats1['total_fetched']} inserted={stats1['total_inserted']} "
            f"skipped={stats1['skipped_duplicates']} observations={stats1['observations']}"
        )
        print(
            f"  run2: id={stats2['run_id']} status={stats2['status']} "
            f"fetched={stats2['total_fetched']} inserted={stats2['total_inserted']} "
            f"skipped={stats2['skipped_duplicates']} observations={stats2['observations']}"
        )
        print(f"  reviews_raw (after both):    {raw_count}")
        print(f"  review_observations (total): {total_obs}")
        print(f"  reviews_processed:           {processed_count}")
        print(f"  junction_dupes:              {junction_dupes}")
        print(f"  processed_dupes_per_raw:     {processed_per_raw}")
        for label, stats in (("run1", stats1), ("run2", stats2)):
            for app in stats["app_results"]:
                print(
                    f"  {label} {app['package_id']}: status={app['status']} "
                    f"fetched={app['fetched_count']} inserted={app['inserted_count']} "
                    f"skipped={app['skipped_count']}"
                )
    finally:
        conn.close()
